import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator, Awaitable, Callable, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, PrivateAttr

from app.ai_request_analysis import (
    OllamaResponseError,
    OllamaUnavailableError,
    generate_analysis,
)
from app.ai_audit_log_analysis import (
    AuditAnalysisQuestionError,
    count_audit_event_types,
    generate_audit_analysis,
    is_clinical_audit_question,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger(__name__)
ADMIN_SESSION_IDLE_TIMEOUT_SECONDS = 10 * 60
ADMIN_SESSION_PROCESS_STARTED_AT = time.time()


@dataclass
class AdminSessionState:
    last_activity: float
    expired: bool = False


admin_sessions: dict[str, AdminSessionState] = {}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    application.state.supabase_client = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    try:
        yield
    finally:
        await application.state.supabase_client.aclose()


app = FastAPI(
    title="Healthcare Agent Safety Framework API",
    version="0.2.0",
    lifespan=lifespan,
)
bearer_scheme = HTTPBearer(auto_error=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


class Role(str, Enum):
    ADMIN = "admin"
    PATIENT = "patient"
    PHYSICIAN = "physician"
    TECHNICIAN = "technician"


class HealthResponse(BaseModel):
    status: str
    database: str


class CurrentUser(BaseModel):
    user_id: int
    email: str
    role: Role
    permissions: list[str]
    name: str | None = None
    _admin_session_key: str | None = PrivateAttr(default=None)
    _password_auth_timestamp: float | None = PrivateAttr(default=None)


class DashboardResponse(BaseModel):
    role: Role
    message: str


class AuthorizationResponse(BaseModel):
    action: str
    allowed: bool


class AdminUserRow(BaseModel):
    user_id: int
    name: str
    email: str
    role: str
    is_active: bool


class AdminInviteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    role: Role


class AdminInviteResponse(BaseModel):
    user_id: int
    email: EmailStr
    role: Role
    invitation_sent: bool


class AdminUserDeactivationResponse(BaseModel):
    user_id: int
    is_active: bool


class AdminPermissionRow(BaseModel):
    role: str
    action: str
    allowed: bool


class AdminAIRequestRow(BaseModel):
    request_id: int
    action: str
    guardrail_decision: Literal["Allow", "Block", "Escalate"]
    created_at: str | None


class AdminAIRequestAnalysisRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class AdminAIRequestAnalysisResponse(BaseModel):
    answer: str
    records_analyzed: int
    record_limit: int


class AdminAIAuditLogAnalysisRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class AdminAIAuditLogAnalysisResponse(BaseModel):
    answer: str
    records_analyzed: int
    record_limit: int
    event_type_counts: dict[str, int]


class AdminAuditLogRow(BaseModel):
    log_id: int
    request_id: int | None
    event_type: str
    created_at: str | None


class AdminOverviewResponse(BaseModel):
    users: list[AdminUserRow] | None
    permissions: list[AdminPermissionRow] | None
    ai_requests: list[AdminAIRequestRow] | None
    audit_logs: list[AdminAuditLogRow] | None
    errors: dict[str, str]


class AuthBootstrapResponse(BaseModel):
    user: CurrentUser
    admin_overview: AdminOverviewResponse | None


class PatientMedicalRecord(BaseModel):
    age: int | None
    medical_condition: str | None
    treatment: str | None


class PatientMedicalRecordResponse(BaseModel):
    record: PatientMedicalRecord | None


class PatientCorrectionRequestResponse(BaseModel):
    request_id: int
    guardrail_decision: Literal["Allow", "Block", "Escalate"]
    approval_status: Literal["Pending", "Approved", "Rejected"]


CorrectionStatusIssue = Literal[
    "missing_approval",
    "multiple_approvals",
    "invalid_approval_status",
    "invalid_guardrail_decision",
    "review_timestamp_mismatch",
]


class PatientCorrectionHistoryItem(BaseModel):
    request_id: int
    created_at: str | None
    guardrail_decision: Literal["Allow", "Block", "Escalate"] | None
    approval_status: Literal["Pending", "Approved", "Rejected"] | None
    reviewed_at: str | None
    # Set when the stored records are inconsistent; the request is then shown
    # as needing attention instead of being given an assumed status.
    status_issue: CorrectionStatusIssue | None = None


class PatientCorrectionHistoryResponse(BaseModel):
    items: list[PatientCorrectionHistoryItem]


PATIENT_CORRECTION_ACTION = "patient_treatment_correction"
PATIENT_CORRECTION_HISTORY_LIMIT = 50
PATIENT_CORRECTION_PRECHECK_LIMIT = 1000
VALID_GUARDRAIL_DECISIONS = {"allow": "Allow", "block": "Block", "escalate": "Escalate"}
VALID_APPROVAL_STATUSES = {"Pending", "Approved", "Rejected"}
CORRECTION_HISTORY_UNAVAILABLE_DETAIL = (
    "Your correction request history is temporarily unavailable."
)
CORRECTION_UNAVAILABLE_DETAIL = "Correction requests are temporarily unavailable."
CORRECTION_UNCONFIRMED_DETAIL = (
    "We could not confirm whether your correction request was submitted. "
    "Do not submit it again yet; check your request history first."
)
# Transport failures raised before the RPC request reaches Supabase. Any other
# transport failure may occur after PostgreSQL has committed the transaction.
CORRECTION_NOT_SENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)
# Gateway statuses returned without a database error code when the upstream
# outcome is unknown.
CORRECTION_UNCONFIRMED_GATEWAY_STATUSES = {502, 504, 520, 524}


async def _request_supabase(
    path: str,
    *,
    api_key: str,
    bearer_token: str | None = None,
    params: dict[str, str] | None = None,
    method: Literal["GET", "POST", "PUT", "PATCH"] = "GET",
    json_body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not supabase_url:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_URL in backend/.env.",
        )

    headers = {"apikey": api_key}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if extra_headers:
        headers.update(extra_headers)

    started_at = time.perf_counter()
    try:
        response = await app.state.supabase_client.request(
            method,
            f"{supabase_url}{path}",
            headers=headers,
            params=params,
            json=json_body,
        )
        logger.info(
            "Supabase request %s completed in %.0f ms (HTTP %s).",
            path,
            (time.perf_counter() - started_at) * 1000,
            response.status_code,
        )
        return response
    except httpx.RequestError as error:
        logger.error(
            "Supabase request %s failed after %.0f ms.",
            path,
            (time.perf_counter() - started_at) * 1000,
        )
        raise HTTPException(
            status_code=503,
            detail="Supabase is temporarily unavailable.",
        ) from error


def _service_role_key() -> str:
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        raise HTTPException(
            status_code=503,
            detail="Backend authentication is not configured. Set SUPABASE_SERVICE_ROLE_KEY in backend/.env.",
        )
    return key


def _validated_auth_claims(access_token: str) -> dict[str, Any]:
    try:
        payload = access_token.split(".")[1]
        payload_bytes = base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)
        )
        claims = json.loads(payload_bytes)
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _password_auth_timestamp(amr: Any) -> float | None:
    if not isinstance(amr, list):
        return None

    timestamps: list[float] = []
    for item in amr:
        if (
            not isinstance(item, dict)
            or item.get("method") != "password"
            or not isinstance(item.get("timestamp"), (int, float))
            or isinstance(item.get("timestamp"), bool)
        ):
            continue
        try:
            timestamp = float(item["timestamp"])
        except OverflowError:
            continue
        if math.isfinite(timestamp):
            timestamps.append(timestamp)
    return max(timestamps) if timestamps else None


def _admin_session_expired() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Admin session expired. Sign in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _active_admin_session(user: CurrentUser) -> AdminSessionState:
    session_key = user._admin_session_key
    password_auth_timestamp = user._password_auth_timestamp
    if session_key is None:
        raise _admin_session_expired()

    now_wall = time.time()
    now_monotonic = time.monotonic()
    session = admin_sessions.get(session_key)
    if session is None:
        if (
            password_auth_timestamp is None
            or password_auth_timestamp < ADMIN_SESSION_PROCESS_STARTED_AT
            or password_auth_timestamp > now_wall + 30
        ):
            raise _admin_session_expired()

        elapsed_since_authentication = max(0, now_wall - password_auth_timestamp)
        session = AdminSessionState(
            last_activity=now_monotonic - elapsed_since_authentication
        )
        admin_sessions[session_key] = session

    if session.expired:
        raise _admin_session_expired()
    if now_monotonic - session.last_activity >= ADMIN_SESSION_IDLE_TIMEOUT_SECONDS:
        session.expired = True
        raise _admin_session_expired()

    return session


async def _verified_auth_user(access_token: str) -> dict[str, Any]:
    anon_key = os.getenv("SUPABASE_KEY", "")
    if not anon_key:
        raise HTTPException(
            status_code=503,
            detail="Backend authentication is not configured. Set SUPABASE_KEY in backend/.env.",
        )

    response = await _request_supabase(
        "/auth/v1/user",
        api_key=anon_key,
        bearer_token=access_token,
    )
    if response.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="Invalid or expired access token.")
    if not response.is_success:
        logger.error("Supabase Auth user validation returned HTTP %s.", response.status_code)
        raise HTTPException(status_code=503, detail="Could not validate the login session.")

    try:
        auth_user = response.json()
        auth_user_id = UUID(auth_user["id"])
        email = auth_user["email"]
    except (ValueError, KeyError, TypeError) as error:
        logger.error("Supabase Auth returned an invalid user response.")
        raise HTTPException(status_code=502, detail="Supabase returned an invalid user response.") from error

    if not isinstance(email, str) or not email:
        raise HTTPException(status_code=403, detail="The authenticated account has no email address.")

    claims = _validated_auth_claims(access_token)
    try:
        session_id = str(UUID(claims["session_id"]))
    except (KeyError, TypeError, ValueError):
        session_id = None

    return {
        "auth_user_id": str(auth_user_id),
        "email": email,
        "session_id": session_id,
        "password_auth_timestamp": _password_auth_timestamp(claims.get("amr")),
    }


async def _load_linked_user(auth_user: dict[str, Any]) -> tuple[int, str, str | None]:
    response = await _request_supabase(
        "/rest/v1/users",
        api_key=_service_role_key(),
        bearer_token=_service_role_key(),
        params={
            "select": "user_id,name,role,is_active",
            "auth_user_id": f"eq.{auth_user['auth_user_id']}",
            "limit": "2",
        },
    )
    if not response.is_success:
        logger.error("Supabase user-profile lookup returned HTTP %s.", response.status_code)
        raise HTTPException(status_code=503, detail="Could not load the application account.")

    try:
        rows = response.json()
    except ValueError as error:
        logger.error("Supabase returned invalid application-account data.")
        raise HTTPException(status_code=502, detail="Supabase returned invalid account data.") from error

    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="Supabase returned invalid account data.")
    if not rows:
        raise HTTPException(
            status_code=403,
            detail="This login is not linked to an application account. Ask an administrator to link it.",
        )
    if len(rows) != 1 or not isinstance(rows[0], dict):
        logger.error("More than one application account is linked to a Supabase Auth user.")
        raise HTTPException(status_code=409, detail="The application account link is ambiguous.")

    try:
        user_id = int(rows[0]["user_id"])
        database_role = str(rows[0]["role"]).strip()
        role = Role(database_role.casefold())
        is_active = rows[0]["is_active"]
        if not isinstance(is_active, bool):
            raise ValueError("Application account active status must be boolean.")
        name = rows[0].get("name")
        if role == Role.PATIENT and (
            not isinstance(name, str) or not name.strip()
        ):
            raise ValueError("Patient account name must be a non-empty string.")
    except (KeyError, TypeError, ValueError) as error:
        logger.error(
            "The linked application account has an invalid id, role, active status, or patient name."
        )
        raise HTTPException(status_code=403, detail="The linked application account is invalid.") from error

    if not is_active:
        raise HTTPException(status_code=403, detail="This application account has been deactivated.")

    return user_id, database_role, name.strip() if isinstance(name, str) else None


async def _load_permissions() -> dict[str, list[str]]:
    response = await _request_supabase(
        "/rest/v1/permissions",
        api_key=_service_role_key(),
        bearer_token=_service_role_key(),
        params={
            "select": "role,action",
            "allowed": "eq.true",
        },
    )
    if not response.is_success:
        logger.error("Supabase permission lookup returned HTTP %s.", response.status_code)
        raise HTTPException(status_code=503, detail="Could not load account permissions.")

    try:
        rows = response.json()
    except ValueError as error:
        logger.error("Supabase returned invalid permission data.")
        raise HTTPException(status_code=502, detail="Supabase returned invalid permission data.") from error

    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("role"), str)
        or not isinstance(row.get("action"), str)
        for row in rows
    ):
        raise HTTPException(status_code=502, detail="Supabase returned invalid permission data.")

    permissions_by_role: dict[str, set[str]] = {}
    for row in rows:
        permissions_by_role.setdefault(row["role"].casefold(), set()).add(row["action"])
    return {
        role: sorted(actions)
        for role, actions in permissions_by_role.items()
    }


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
) -> CurrentUser:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="A valid Supabase access token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_user = await _verified_auth_user(credentials.credentials)
    (user_id, database_role, name), permissions_by_role = await asyncio.gather(
        _load_linked_user(auth_user),
        _load_permissions(),
    )
    permissions = permissions_by_role.get(database_role.casefold(), [])

    current_user = CurrentUser(
        user_id=user_id,
        email=auth_user["email"],
        role=Role(database_role.casefold()),
        permissions=permissions,
        name=name if database_role.casefold() == Role.PATIENT.value else None,
    )
    if current_user.role == Role.ADMIN:
        session_id = auth_user.get("session_id")
        if isinstance(session_id, str):
            current_user._admin_session_key = hashlib.sha256(
                f"{auth_user['auth_user_id']}:{session_id}".encode("utf-8")
            ).hexdigest()
        current_user._password_auth_timestamp = auth_user.get(
            "password_auth_timestamp"
        )
        _active_admin_session(current_user)

    return current_user


async def require_admin(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role is required.")
    return user


def require_permission(action: str):
    async def check_permission(
        user: Annotated[CurrentUser, Depends(require_admin)],
    ) -> CurrentUser:
        if action not in user.permissions:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
        return user

    return check_permission


async def require_patient(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if user.role != Role.PATIENT:
        raise HTTPException(status_code=403, detail="Patient role is required.")
    return user


async def _admin_table_rows(
    table: str,
    *,
    select: str,
    order: str,
    limit: int,
    offset: int,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    query_params = {
        "select": select,
        "order": order,
        "limit": str(limit),
        "offset": str(offset),
    }
    if filters:
        query_params.update(filters)

    service_key = _service_role_key()
    response = await _request_supabase(
        f"/rest/v1/{table}",
        api_key=service_key,
        bearer_token=service_key,
        params=query_params,
    )
    if not response.is_success:
        logger.error(
            "Admin read for %s returned HTTP %s.",
            table,
            response.status_code,
        )
        raise HTTPException(status_code=503, detail=f"Could not load {table}.")

    try:
        rows = response.json()
    except ValueError as error:
        logger.error("Supabase returned invalid data for %s.", table)
        raise HTTPException(status_code=502, detail=f"Supabase returned invalid {table} data.") from error

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=502, detail=f"Supabase returned invalid {table} data.")
    return rows


async def _load_admin_overview_section(
    user: CurrentUser,
    key: str,
    permission: str,
    load_rows: Callable[[], Awaitable[list[Any]]],
) -> tuple[str, list[Any] | None, str | None]:
    if permission not in user.permissions:
        return key, None, "You do not have permission to view this section."

    try:
        return key, await load_rows(), None
    except HTTPException as error:
        return key, None, str(error.detail)


async def _build_admin_overview(user: CurrentUser) -> AdminOverviewResponse:
    async def load_users() -> list[AdminUserRow]:
        rows = await _admin_table_rows(
            "users",
            select="user_id,name,email,role,is_active",
            order="user_id.asc",
            limit=50,
            offset=0,
        )
        return [AdminUserRow.model_validate(row) for row in rows]

    async def load_permissions() -> list[AdminPermissionRow]:
        rows = await _admin_table_rows(
            "permissions",
            select="role,action,allowed",
            order="role.asc,action.asc",
            limit=100,
            offset=0,
        )
        return [AdminPermissionRow.model_validate(row) for row in rows]

    async def load_ai_requests() -> list[AdminAIRequestRow]:
        rows = await _admin_table_rows(
            "ai_requests",
            select="request_id,action,guardrail_decision,created_at",
            order="created_at.desc",
            limit=25,
            offset=0,
        )
        return _admin_ai_request_models(rows)

    async def load_audit_logs() -> list[AdminAuditLogRow]:
        rows = await _admin_table_rows(
            "audit_logs",
            select="log_id,request_id,event_type,created_at",
            order="log_id.desc",
            limit=25,
            offset=0,
        )
        return [AdminAuditLogRow.model_validate(row) for row in rows]

    section_results = await asyncio.gather(
        _load_admin_overview_section(user, "users", "manage_users", load_users),
        _load_admin_overview_section(user, "permissions", "manage_users", load_permissions),
        _load_admin_overview_section(
            user,
            "ai_requests",
            "monitor_ai_requests",
            load_ai_requests,
        ),
        _load_admin_overview_section(user, "audit_logs", "view_audit_logs", load_audit_logs),
    )
    sections = {key: data for key, data, _ in section_results}
    errors = {key: error for key, _, error in section_results if error is not None}
    return AdminOverviewResponse(**sections, errors=errors)


def _admin_ai_request_models(rows: list[dict[str, Any]]) -> list[AdminAIRequestRow]:
    valid_decisions = {"allow": "Allow", "block": "Block", "escalate": "Escalate"}
    safe_rows: list[AdminAIRequestRow] = []
    for row in rows:
        decision = row.get("guardrail_decision")
        if not isinstance(decision, str) or decision.casefold() not in valid_decisions:
            logger.error("An AI request contains an unsupported guardrail decision.")
            raise HTTPException(
                status_code=502,
                detail="An AI request has an unsupported guardrail decision.",
            )
        safe_rows.append(
            AdminAIRequestRow.model_validate(
                {**row, "guardrail_decision": valid_decisions[decision.casefold()]}
            )
        )
    return safe_rows


@app.get("/api/health", response_model=HealthResponse)
async def check_health() -> HealthResponse:
    anon_key = os.getenv("SUPABASE_KEY", "")
    if not anon_key:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Set SUPABASE_KEY in backend/.env.",
        )

    response = await _request_supabase(
        "/rest/v1/users",
        api_key=anon_key,
        params={"select": "*", "limit": "0"},
    )
    if not response.is_success:
        logger.error("Supabase connection check returned HTTP status %s.", response.status_code)
        raise HTTPException(
            status_code=503,
            detail="Supabase database check failed. Verify the project URL, key, and access to the existing public.users table.",
        )

    return HealthResponse(status="ok", database="connected")


@app.get("/api/auth/me", response_model=CurrentUser)
async def get_my_account(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    return user


@app.get("/api/dashboard/{role}", response_model=DashboardResponse)
async def get_role_dashboard(
    role: Role,
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> DashboardResponse:
    if role != user.role:
        raise HTTPException(status_code=403, detail="You cannot access another role's dashboard.")

    return DashboardResponse(
        role=user.role,
        message=f"Welcome. The {user.role.value.title()} workspace is ready for its module.",
    )


@app.get(
    "/api/patient/my-record",
    response_model=PatientMedicalRecordResponse,
)
async def get_patient_medical_record(
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> PatientMedicalRecordResponse:
    try:
        service_key = _service_role_key()
        response = await _request_supabase(
            "/rest/v1/patients",
            api_key=service_key,
            bearer_token=service_key,
            params={
                "select": "age,medical_condition,treatment",
                "user_id": f"eq.{user.user_id}",
                "limit": "2",
            },
        )
    except HTTPException as error:
        logger.error(
            "Patient medical-record lookup failed before receiving a Supabase response (HTTP %s).",
            error.status_code,
        )
        raise HTTPException(
            status_code=503,
            detail="Medical records are temporarily unavailable.",
        ) from error
    except httpx.HTTPError as error:
        logger.error(
            "Patient medical-record lookup encountered a Supabase transport failure (%s).",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Medical records are temporarily unavailable.",
        ) from error

    if not response.is_success:
        logger.error(
            "Patient medical-record lookup returned HTTP %s.",
            response.status_code,
        )
        raise HTTPException(
            status_code=503,
            detail="Medical records are temporarily unavailable.",
        )

    try:
        rows = response.json()
    except ValueError as error:
        logger.error("Supabase returned invalid patient medical-record data.")
        raise HTTPException(
            status_code=502,
            detail="Patient medical-record data is invalid.",
        ) from error

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        logger.error("Supabase returned an invalid patient medical-record result.")
        raise HTTPException(
            status_code=502,
            detail="Patient medical-record data is invalid.",
        )
    if len(rows) > 1:
        logger.error(
            "Multiple patient medical records are linked to application user %s.",
            user.user_id,
        )
        raise HTTPException(
            status_code=409,
            detail="Multiple medical records are linked to this account.",
        )
    if not rows:
        return PatientMedicalRecordResponse(record=None)

    row = rows[0]
    age = row.get("age")
    medical_condition = row.get("medical_condition")
    treatment = row.get("treatment")
    if (
        age is not None
        and (not isinstance(age, int) or isinstance(age, bool))
    ) or any(
        value is not None and not isinstance(value, str)
        for value in (medical_condition, treatment)
    ):
        logger.error("Supabase returned invalid patient medical-record fields.")
        raise HTTPException(
            status_code=502,
            detail="Patient medical-record data is invalid.",
        )

    return PatientMedicalRecordResponse(
        record=PatientMedicalRecord(
            age=age,
            medical_condition=medical_condition,
            treatment=treatment,
        )
    )


async def _patient_service_rows(
    table: str,
    params: dict[str, str],
    *,
    unavailable_detail: str,
) -> list[dict[str, Any]]:
    try:
        service_key = _service_role_key()
        response = await _request_supabase(
            f"/rest/v1/{table}",
            api_key=service_key,
            bearer_token=service_key,
            params=params,
        )
    except (HTTPException, httpx.HTTPError) as error:
        cause = error.__cause__ if isinstance(error, HTTPException) else error
        logger.error(
            "Patient %s lookup failed before receiving a Supabase response (%s).",
            table,
            type(cause or error).__name__,
        )
        raise HTTPException(status_code=503, detail=unavailable_detail) from error

    if not response.is_success:
        logger.error(
            "Patient %s lookup returned HTTP %s.",
            table,
            response.status_code,
        )
        raise HTTPException(status_code=503, detail=unavailable_detail)

    try:
        rows = response.json()
    except ValueError as error:
        logger.error("Supabase returned invalid patient %s data.", table)
        raise HTTPException(status_code=502, detail=unavailable_detail) from error

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        logger.error("Supabase returned an invalid patient %s result.", table)
        raise HTTPException(status_code=502, detail=unavailable_detail)
    return rows


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


async def _load_own_correction_requests(
    user_id: int,
    *,
    limit: int,
    unavailable_detail: str,
) -> list[dict[str, Any]]:
    # The requester filter comes only from the verified CurrentUser. The
    # proposed value (request_details) and decision_reason are never selected.
    rows = await _patient_service_rows(
        "ai_requests",
        {
            "select": "request_id,requested_by,guardrail_decision,created_at",
            "requested_by": f"eq.{user_id}",
            "action": f"eq.{PATIENT_CORRECTION_ACTION}",
            "order": "created_at.desc,request_id.desc",
            "limit": str(limit),
        },
        unavailable_detail=unavailable_detail,
    )
    for row in rows:
        if (
            not _is_strict_int(row.get("request_id"))
            or row.get("requested_by") != user_id
            or not isinstance(row.get("created_at"), (str, type(None)))
        ):
            logger.error("Supabase returned an invalid or foreign correction request row.")
            raise HTTPException(status_code=502, detail=unavailable_detail)
    return rows


async def _load_correction_approvals(
    request_ids: list[int],
    *,
    unavailable_detail: str,
) -> dict[int, list[dict[str, Any]]]:
    if not request_ids:
        return {}
    # reviewer_id is intentionally not selected.
    rows = await _patient_service_rows(
        "approvals",
        {
            "select": "request_id,status,reviewed_at",
            "request_id": f"in.({','.join(str(request_id) for request_id in request_ids)})",
        },
        unavailable_detail=unavailable_detail,
    )
    approvals: dict[int, list[dict[str, Any]]] = {}
    requested = set(request_ids)
    for row in rows:
        request_id = row.get("request_id")
        if not _is_strict_int(request_id) or request_id not in requested:
            logger.error("Supabase returned an invalid or unrelated approval row.")
            raise HTTPException(status_code=502, detail=unavailable_detail)
        approvals.setdefault(request_id, []).append(row)
    return approvals


def _correction_history_item(
    request: dict[str, Any],
    approvals: list[dict[str, Any]],
) -> PatientCorrectionHistoryItem:
    issue: CorrectionStatusIssue | None = None

    decision_value = request.get("guardrail_decision")
    guardrail_decision = (
        VALID_GUARDRAIL_DECISIONS.get(decision_value.casefold())
        if isinstance(decision_value, str)
        else None
    )
    if guardrail_decision is None:
        issue = "invalid_guardrail_decision"

    approval_status: str | None = None
    reviewed_at: str | None = None
    if not approvals:
        issue = issue or "missing_approval"
    elif len(approvals) > 1:
        issue = issue or "multiple_approvals"
    else:
        approval = approvals[0]
        status = approval.get("status")
        reviewed_value = approval.get("reviewed_at")
        if status not in VALID_APPROVAL_STATUSES or not isinstance(
            reviewed_value, (str, type(None))
        ):
            issue = issue or "invalid_approval_status"
        else:
            approval_status = status
            reviewed_at = reviewed_value
            if (status == "Pending") != (reviewed_at is None):
                issue = issue or "review_timestamp_mismatch"

    if issue is not None:
        logger.warning(
            "Patient correction request %s has inconsistent records (%s).",
            request["request_id"],
            issue,
        )

    return PatientCorrectionHistoryItem(
        request_id=request["request_id"],
        created_at=request.get("created_at"),
        guardrail_decision=guardrail_decision,
        approval_status=approval_status,
        reviewed_at=reviewed_at,
        status_issue=issue,
    )


async def _blocking_correction_request_id(user_id: int) -> int | None:
    """Return the newest own correction request that is not finally reviewed.

    A request blocks a new submission when its approval is Pending, missing,
    duplicated, or has an unrecognized status. This check improves the
    Patient experience but is not race-free on its own; the database function
    must enforce the same rule atomically.
    """
    requests = await _load_own_correction_requests(
        user_id,
        limit=PATIENT_CORRECTION_PRECHECK_LIMIT,
        unavailable_detail=CORRECTION_UNAVAILABLE_DETAIL,
    )
    request_ids = [row["request_id"] for row in requests]
    approvals = await _load_correction_approvals(
        request_ids,
        unavailable_detail=CORRECTION_UNAVAILABLE_DETAIL,
    )
    for request_id in request_ids:
        request_approvals = approvals.get(request_id, [])
        if len(request_approvals) != 1 or request_approvals[0].get("status") not in {
            "Approved",
            "Rejected",
        }:
            return request_id
    return None


@app.get(
    "/api/patient/correction-requests",
    response_model=PatientCorrectionHistoryResponse,
)
async def get_patient_correction_requests(
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> PatientCorrectionHistoryResponse:
    if "view_own_correction_requests" not in user.permissions:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view correction requests.",
        )

    requests = await _load_own_correction_requests(
        user.user_id,
        limit=PATIENT_CORRECTION_HISTORY_LIMIT,
        unavailable_detail=CORRECTION_HISTORY_UNAVAILABLE_DETAIL,
    )
    approvals = await _load_correction_approvals(
        [row["request_id"] for row in requests],
        unavailable_detail=CORRECTION_HISTORY_UNAVAILABLE_DETAIL,
    )
    return PatientCorrectionHistoryResponse(
        items=[
            _correction_history_item(row, approvals.get(row["request_id"], []))
            for row in requests
        ]
    )


@app.post(
    "/api/patient/correction-requests",
    response_model=PatientCorrectionRequestResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "proposed_treatment": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            }
                        },
                        "required": ["proposed_treatment"],
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
async def submit_patient_correction_request(
    request: Request,
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> PatientCorrectionRequestResponse:
    if "submit_record_correction" not in user.permissions:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to submit correction requests.",
        )

    try:
        body = await request.json()
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="The correction request must contain valid JSON.",
        ) from error

    if (
        not isinstance(body, dict)
        or set(body) != {"proposed_treatment"}
        or not isinstance(body.get("proposed_treatment"), str)
    ):
        raise HTTPException(
            status_code=422,
            detail="Provide only a proposed_treatment string.",
        )

    proposed_treatment = body["proposed_treatment"]
    if (
        not proposed_treatment.strip()
        or len(proposed_treatment) > 2000
    ):
        raise HTTPException(
            status_code=422,
            detail="The proposed treatment must contain 1 to 2000 characters.",
        )

    # Fails closed: if existing requests cannot be checked, nothing is submitted.
    blocking_request_id = await _blocking_correction_request_id(user.user_id)
    if blocking_request_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Correction request #{blocking_request_id} is still awaiting "
                "Physician review. You can submit a new correction after it "
                "has been reviewed."
            ),
        )

    try:
        service_key = _service_role_key()
        response = await _request_supabase(
            "/rest/v1/rpc/submit_patient_treatment_correction",
            api_key=service_key,
            method="POST",
            json_body={
                "p_requested_by": user.user_id,
                "p_proposed_treatment": proposed_treatment,
            },
            extra_headers={"Authorization": f"Bearer {service_key}"},
        )
    except (HTTPException, httpx.HTTPError) as error:
        # _request_supabase wraps transport failures in an HTTPException.
        transport_error = (
            error.__cause__ if isinstance(error, HTTPException) else error
        )
        if isinstance(transport_error, httpx.RequestError) and not isinstance(
            transport_error, CORRECTION_NOT_SENT_ERRORS
        ):
            logger.error(
                "Patient correction RPC outcome is unconfirmed after a transport failure (%s).",
                type(transport_error).__name__,
            )
            raise HTTPException(
                status_code=504,
                detail=CORRECTION_UNCONFIRMED_DETAIL,
            ) from error

        logger.error(
            "Patient correction RPC request was not sent (%s).",
            type(transport_error or error).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail=CORRECTION_UNAVAILABLE_DETAIL,
        ) from error

    if not response.is_success:
        error_code = ""
        error_message = ""
        try:
            rpc_error = response.json()
            if isinstance(rpc_error, dict):
                code = rpc_error.get("code")
                message = rpc_error.get("message")
                if isinstance(code, str):
                    error_code = code
                if isinstance(message, str):
                    error_message = message
        except ValueError:
            pass

        logger.error(
            "Patient correction RPC returned HTTP %s (database code %s).",
            response.status_code,
            error_code or "unavailable",
        )
        if (
            not error_code
            and response.status_code in CORRECTION_UNCONFIRMED_GATEWAY_STATUSES
        ):
            raise HTTPException(
                status_code=504,
                detail=CORRECTION_UNCONFIRMED_DETAIL,
            )
        if error_code == "PT409":
            # Raised by the database function (migration 007) when a correction
            # request for this Patient is already awaiting review.
            raise HTTPException(
                status_code=409,
                detail=(
                    "A correction request is already awaiting Physician review. "
                    "You can submit a new correction after it has been reviewed."
                ),
            )
        if error_code == "22023":
            raise HTTPException(
                status_code=422,
                detail="The correction request is invalid or does not change the current value.",
            )
        if error_code == "42501":
            if error_message == (
                "No unique active assigned Physician is authorized to review this request."
            ):
                raise HTTPException(
                    status_code=409,
                    detail="An authorized Physician is unavailable to review this request.",
                )
            raise HTTPException(
                status_code=403,
                detail="The correction request is not authorized.",
            )
        # A database error code means the RPC transaction was rolled back.
        raise HTTPException(
            status_code=503,
            detail=CORRECTION_UNAVAILABLE_DETAIL,
        )

    # From here the RPC succeeded, so the request may have been recorded even
    # if its confirmation cannot be validated.
    try:
        result = response.json()
    except ValueError as error:
        logger.error("Patient correction RPC returned invalid response data.")
        raise HTTPException(
            status_code=502,
            detail=CORRECTION_UNCONFIRMED_DETAIL,
        ) from error

    if (
        not isinstance(result, dict)
        or not isinstance(result.get("request_id"), int)
        or isinstance(result.get("request_id"), bool)
        or result.get("guardrail_decision") != "Escalate"
        or result.get("approval_status") != "Pending"
    ):
        logger.error("Patient correction RPC returned an unexpected response shape.")
        raise HTTPException(
            status_code=502,
            detail=CORRECTION_UNCONFIRMED_DETAIL,
        )

    return PatientCorrectionRequestResponse(
        request_id=result["request_id"],
        guardrail_decision="Escalate",
        approval_status="Pending",
    )


@app.get("/api/authorization/{action}", response_model=AuthorizationResponse)
async def check_action_permission(
    action: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> AuthorizationResponse:
    allowed = action in user.permissions
    return AuthorizationResponse(action=action, allowed=allowed)


@app.post("/api/admin/session/activity")
async def record_admin_session_activity(
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> dict[str, bool]:
    session = _active_admin_session(user)
    session.last_activity = time.monotonic()
    return {"active": True}


@app.post("/api/admin/session/logout")
async def end_admin_session(
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> dict[str, bool]:
    session = _active_admin_session(user)
    session.expired = True
    return {"signed_out": True}


@app.get("/api/admin/user-management/access")
async def check_user_management_access(
    user: Annotated[CurrentUser, Depends(require_permission("manage_users"))],
) -> dict[str, bool]:
    return {"authorized": True}


@app.get("/api/admin/audit-logs/access")
async def check_audit_log_access(
    user: Annotated[CurrentUser, Depends(require_permission("view_audit_logs"))],
) -> dict[str, bool]:
    return {"authorized": True}


@app.get("/api/admin/users", response_model=list[AdminUserRow])
async def get_admin_users(
    _user: Annotated[CurrentUser, Depends(require_permission("manage_users"))],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AdminUserRow]:
    rows = await _admin_table_rows(
        "users",
        select="user_id,name,email,role,is_active",
        order="user_id.asc",
        limit=limit,
        offset=offset,
    )
    return [AdminUserRow.model_validate(row) for row in rows]


@app.post(
    "/api/admin/users/{target_user_id}/deactivate",
    response_model=AdminUserDeactivationResponse,
)
async def deactivate_admin_user(
    target_user_id: int,
    user: Annotated[CurrentUser, Depends(require_permission("manage_users"))],
) -> AdminUserDeactivationResponse:
    if target_user_id == user.user_id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")

    target_rows = await _admin_table_rows(
        "users",
        select="user_id,auth_user_id,is_active",
        order="user_id.asc",
        limit=1,
        offset=0,
        filters={"user_id": f"eq.{target_user_id}"},
    )
    if not target_rows:
        raise HTTPException(status_code=404, detail="The application account was not found.")

    target_account = target_rows[0]
    if not isinstance(target_account.get("is_active"), bool):
        logger.error("Application account %s has an invalid active status.", target_user_id)
        raise HTTPException(status_code=502, detail="Supabase returned invalid account data.")

    auth_user_id_value = target_account.get("auth_user_id")
    auth_user_id: str | None = None
    if auth_user_id_value is not None:
        try:
            auth_user_id = str(UUID(auth_user_id_value))
        except (ValueError, TypeError) as error:
            logger.error("Application account %s has an invalid Auth user link.", target_user_id)
            raise HTTPException(status_code=502, detail="Supabase returned invalid account data.") from error

    if target_account["is_active"]:
        update_response = await _request_supabase(
            "/rest/v1/users",
            api_key=_service_role_key(),
            bearer_token=_service_role_key(),
            params={
                "user_id": f"eq.{target_user_id}",
                "is_active": "eq.true",
            },
            method="PATCH",
            json_body={"is_active": False},
            extra_headers={"Prefer": "return=representation"},
        )
        if not update_response.is_success:
            logger.error(
                "Deactivation of application account %s returned HTTP %s.",
                target_user_id,
                update_response.status_code,
            )
            raise HTTPException(status_code=503, detail="Could not deactivate the application account.")

        try:
            updated_rows = update_response.json()
        except ValueError as error:
            logger.error("Supabase returned invalid deactivation data for account %s.", target_user_id)
            raise HTTPException(status_code=502, detail="Supabase returned invalid account data.") from error
        if not isinstance(updated_rows, list) or len(updated_rows) != 1:
            raise HTTPException(status_code=409, detail="The account changed before it could be deactivated.")

    auth_ban_succeeded = True
    if auth_user_id:
        auth_response = await _request_supabase(
            f"/auth/v1/admin/users/{auth_user_id}",
            api_key=_service_role_key(),
            bearer_token=_service_role_key(),
            method="PUT",
            json_body={"ban_duration": "876000h"},
        )
        auth_ban_succeeded = auth_response.is_success
        if not auth_ban_succeeded:
            logger.error(
                "Application access was revoked for account %s, but Supabase Auth returned HTTP %s while blocking sign-in.",
                target_user_id,
                auth_response.status_code,
            )

    audit_succeeded = True
    if target_account["is_active"]:
        audit_response = await _request_supabase(
            "/rest/v1/audit_logs",
            api_key=_service_role_key(),
            bearer_token=_service_role_key(),
            method="POST",
            json_body={
                "user_id": user.user_id,
                "event_type": "admin.user.deactivated",
                "details": json.dumps(
                    {"target_user_id": target_user_id},
                    separators=(",", ":"),
                ),
            },
            extra_headers={"Prefer": "return=minimal"},
        )
        if not audit_response.is_success:
            audit_succeeded = False
            logger.error(
                "Application account %s was deactivated, but the audit insert returned HTTP %s.",
                target_user_id,
                audit_response.status_code,
            )

    if not auth_ban_succeeded and not audit_succeeded:
        raise HTTPException(
            status_code=502,
            detail="Application access was revoked, but Supabase Auth could not block sign-in and the audit event could not be recorded. Contact an administrator.",
        )
    if not auth_ban_succeeded:
        raise HTTPException(
            status_code=502,
            detail="Application access was revoked, but Supabase Auth could not block sign-in. Retry this action or contact an administrator.",
        )
    if not audit_succeeded:
        raise HTTPException(
            status_code=502,
            detail="The account was deactivated, but its audit event could not be recorded. Contact an administrator before retrying.",
        )

    return AdminUserDeactivationResponse(user_id=target_user_id, is_active=False)


@app.post("/api/admin/users/invite", response_model=AdminInviteResponse, status_code=201)
async def invite_admin_user(
    request: AdminInviteRequest,
    _user: Annotated[CurrentUser, Depends(require_permission("manage_users"))],
) -> AdminInviteResponse:
    service_key = _service_role_key()
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")

    normalized_email = str(request.email).strip().casefold()
    normalized_name = request.name.strip()
    if not normalized_name:
        raise HTTPException(status_code=422, detail="Name must not be blank.")

    existing_rows = await _admin_table_rows(
        "users",
        select="user_id",
        order="user_id.asc",
        limit=2,
        offset=0,
        filters={"email": f"ilike.{normalized_email}"},
    )
    if existing_rows:
        raise HTTPException(
            status_code=409,
            detail="An application account with that email already exists.",
        )

    role_label = request.role.value.title()
    invite_response = await _request_supabase(
        "/auth/v1/invite",
        api_key=service_key,
        bearer_token=service_key,
        params={"redirect_to": f"{frontend_url}/set-password"},
        method="POST",
        json_body={
            "email": normalized_email,
            "data": {"name": normalized_name},
        },
    )
    if invite_response.status_code in (400, 409, 422):
        logger.warning(
            "Supabase rejected an invitation for a new application account (HTTP %s).",
            invite_response.status_code,
        )
        raise HTTPException(
            status_code=409,
            detail="Supabase could not invite this email. It may already have an Auth account; ask the user to use account recovery or contact an administrator.",
        )
    if not invite_response.is_success:
        logger.error("Supabase Auth invitation returned HTTP %s.", invite_response.status_code)
        raise HTTPException(status_code=502, detail="Supabase could not send the invitation.")

    try:
        auth_user = invite_response.json()
        auth_user_id = str(UUID(auth_user["id"]))
    except (ValueError, KeyError, TypeError) as error:
        logger.error("Supabase Auth returned an invalid invitation response.")
        raise HTTPException(status_code=502, detail="Supabase returned an invalid invitation response.") from error

    insert_response = await _request_supabase(
        "/rest/v1/users",
        api_key=service_key,
        bearer_token=service_key,
        method="POST",
        json_body={
            "name": normalized_name,
            "email": normalized_email,
            "role": role_label,
            "auth_user_id": auth_user_id,
        },
        extra_headers={"Prefer": "return=representation"},
    )
    if not insert_response.is_success:
        logger.error(
            "Auth invitation was created but the application user insert failed (HTTP %s).",
            insert_response.status_code,
        )
        raise HTTPException(
            status_code=502,
            detail="The Auth invitation was created, but the application account could not be saved. Contact an administrator to complete the account link before the invite is used.",
        )

    try:
        inserted_rows = insert_response.json()
        inserted_user = inserted_rows[0]
        user_id = int(inserted_user["user_id"])
    except (ValueError, KeyError, TypeError, IndexError) as error:
        logger.error("Supabase returned an invalid application-user insert response.")
        raise HTTPException(
            status_code=502,
            detail="The Auth invitation and account may exist, but the saved account could not be confirmed. Contact an administrator.",
        ) from error

    audit_response = await _request_supabase(
        "/rest/v1/audit_logs",
        api_key=service_key,
        bearer_token=service_key,
        method="POST",
        json_body={
            "user_id": _user.user_id,
            "event_type": "admin.user.invited",
            "details": json.dumps(
                {"target_user_id": user_id, "assigned_role": role_label},
                separators=(",", ":"),
            ),
        },
        extra_headers={"Prefer": "return=minimal"},
    )
    if not audit_response.is_success:
        logger.error(
            "User %s was invited and created, but the append-only audit insert failed (HTTP %s).",
            user_id,
            audit_response.status_code,
        )
        raise HTTPException(
            status_code=502,
            detail="The invitation was sent and the account was created, but its audit event could not be recorded. Contact an administrator before retrying.",
        )

    return AdminInviteResponse(
        user_id=user_id,
        email=normalized_email,
        role=request.role,
        invitation_sent=True,
    )


@app.get("/api/admin/permissions", response_model=list[AdminPermissionRow])
async def get_admin_permissions(
    _user: Annotated[CurrentUser, Depends(require_permission("manage_users"))],
    limit: int = Query(default=100, ge=1, le=250),
    offset: int = Query(default=0, ge=0),
) -> list[AdminPermissionRow]:
    rows = await _admin_table_rows(
        "permissions",
        select="role,action,allowed",
        order="role.asc,action.asc",
        limit=limit,
        offset=offset,
    )
    return [AdminPermissionRow.model_validate(row) for row in rows]


@app.get("/api/admin/ai-requests", response_model=list[AdminAIRequestRow])
async def get_admin_ai_requests(
    _user: Annotated[CurrentUser, Depends(require_permission("monitor_ai_requests"))],
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AdminAIRequestRow]:
    return await _load_admin_ai_requests(limit=limit, offset=offset)


async def _load_admin_ai_requests(
    *,
    limit: int,
    offset: int = 0,
) -> list[AdminAIRequestRow]:
    rows = await _admin_table_rows(
        "ai_requests",
        select="request_id,action,guardrail_decision,created_at",
        order="created_at.desc",
        limit=limit,
        offset=offset,
    )
    return _admin_ai_request_models(rows)


async def _load_admin_ai_analysis_records(
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows = await _admin_table_rows(
        "ai_requests",
        select="request_id,action,guardrail_decision,created_at,decision_reason",
        order="created_at.desc",
        limit=limit,
        offset=0,
    )
    safe_rows = _admin_ai_request_models(rows)
    return [
        {
            **safe_row.model_dump(mode="json"),
            "decision_reason": raw_row.get("decision_reason"),
        }
        for raw_row, safe_row in zip(rows, safe_rows, strict=True)
    ]


@app.post(
    "/api/admin/ai-requests/analyze",
    response_model=AdminAIRequestAnalysisResponse,
)
async def analyze_admin_ai_requests(
    request: AdminAIRequestAnalysisRequest,
    _user: Annotated[CurrentUser, Depends(require_permission("monitor_ai_requests"))],
) -> AdminAIRequestAnalysisResponse:
    question = request.question.strip()
    if len(question) < 3:
        raise HTTPException(status_code=422, detail="Enter a question with at least 3 characters.")

    records = await _load_admin_ai_analysis_records(limit=100)
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b").strip()
    try:
        ollama_host = urlsplit(ollama_url)
    except ValueError as error:
        raise HTTPException(status_code=503, detail="OLLAMA_URL is invalid.") from error
    if (
        ollama_host.scheme != "http"
        or ollama_host.hostname not in {"localhost", "127.0.0.1", "::1"}
        or ollama_host.username is not None
        or ollama_host.password is not None
    ):
        raise HTTPException(
            status_code=503,
            detail="OLLAMA_URL must point to a local Ollama server to keep request data on this computer.",
        )
    if not model:
        raise HTTPException(status_code=503, detail="Set OLLAMA_MODEL in backend/.env.")

    try:
        answer = await generate_analysis(
            app.state.supabase_client,
            ollama_url=ollama_url,
            model=model,
            question=question,
            records=records,
        )
    except OllamaUnavailableError as error:
        logger.warning("Local Ollama analysis is unavailable: %s", error or "connection failure")
        raise HTTPException(
            status_code=503,
            detail="The local AI model is unavailable. Start Ollama and confirm the configured model is installed.",
        ) from error
    except OllamaResponseError as error:
        logger.error("Ollama returned an invalid analysis response.")
        raise HTTPException(
            status_code=502,
            detail="The local AI model returned an invalid response. Try again.",
        ) from error

    return AdminAIRequestAnalysisResponse(
        answer=answer,
        records_analyzed=len(records),
        record_limit=100,
    )


@app.get("/api/admin/audit-logs", response_model=list[AdminAuditLogRow])
async def get_admin_audit_logs(
    _user: Annotated[CurrentUser, Depends(require_permission("view_audit_logs"))],
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[AdminAuditLogRow]:
    rows = await _admin_table_rows(
        "audit_logs",
        select="log_id,request_id,event_type,created_at",
        order="log_id.desc",
        limit=limit,
        offset=offset,
    )
    return [AdminAuditLogRow.model_validate(row) for row in rows]


@app.post(
    "/api/admin/audit-logs/analyze",
    response_model=AdminAIAuditLogAnalysisResponse,
)
async def analyze_admin_audit_logs(
    request: AdminAIAuditLogAnalysisRequest,
    _user: Annotated[CurrentUser, Depends(require_permission("view_audit_logs"))],
) -> AdminAIAuditLogAnalysisResponse:
    question = request.question.strip()
    if len(question) < 3:
        raise HTTPException(status_code=422, detail="Enter a question with at least 3 characters.")
    if is_clinical_audit_question(question):
        raise HTTPException(
            status_code=422,
            detail=(
                "The Admin AI cannot access patient medical data. Audit-log "
                "analysis is limited to approved event metadata and cannot "
                "provide patient or clinical information."
            ),
        )

    records = await _admin_table_rows(
        "audit_logs",
        select="log_id,request_id,event_type,created_at",
        order="log_id.desc",
        limit=100,
        offset=0,
    )
    try:
        event_type_counts = count_audit_event_types(records)
    except ValueError as error:
        logger.error("Supabase returned invalid audit analysis data.")
        raise HTTPException(
            status_code=502,
            detail="Supabase returned invalid audit log data.",
        ) from error
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b").strip()
    try:
        ollama_host = urlsplit(ollama_url)
    except ValueError as error:
        raise HTTPException(status_code=503, detail="OLLAMA_URL is invalid.") from error
    if (
        ollama_host.scheme != "http"
        or ollama_host.hostname not in {"localhost", "127.0.0.1", "::1"}
        or ollama_host.username is not None
        or ollama_host.password is not None
    ):
        raise HTTPException(
            status_code=503,
            detail="OLLAMA_URL must point to a local Ollama server to keep audit data on this computer.",
        )
    if not model:
        raise HTTPException(status_code=503, detail="Set OLLAMA_MODEL in backend/.env.")

    try:
        answer = await generate_audit_analysis(
            app.state.supabase_client,
            ollama_url=ollama_url,
            model=model,
            question=question,
            records=records,
            event_type_counts=event_type_counts,
        )
    except AuditAnalysisQuestionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        logger.error("Supabase returned invalid audit analysis data.")
        raise HTTPException(
            status_code=502,
            detail="Supabase returned invalid audit log data.",
        ) from error
    except OllamaUnavailableError as error:
        logger.warning("Local Ollama audit analysis is unavailable.")
        raise HTTPException(
            status_code=503,
            detail="The local AI model is unavailable. Start Ollama and confirm the configured model is installed.",
        ) from error
    except OllamaResponseError as error:
        logger.error("Ollama returned an invalid audit analysis response.")
        raise HTTPException(
            status_code=502,
            detail="The local AI model returned an invalid response. Try again.",
        ) from error

    return AdminAIAuditLogAnalysisResponse(
        answer=answer,
        records_analyzed=len(records),
        record_limit=100,
        event_type_counts=event_type_counts,
    )


@app.get("/api/admin/overview", response_model=AdminOverviewResponse)
async def get_admin_overview(
    user: Annotated[CurrentUser, Depends(require_admin)],
) -> AdminOverviewResponse:
    return await _build_admin_overview(user)


@app.get("/api/auth/bootstrap", response_model=AuthBootstrapResponse)
async def bootstrap_session(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> AuthBootstrapResponse:
    admin_overview = None
    if user.role == Role.ADMIN:
        admin_overview = await _build_admin_overview(user)
    return AuthBootstrapResponse(user=user, admin_overview=admin_overview)
