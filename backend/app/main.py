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
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, PrivateAttr

from app.action_gateway import ASSISTANT_PERMISSION, IntentProposal, decide, explain, parse_proposal
from app import chat_history, chat_state, safety_rules
from app.patient_chat import (
    RECORD_MISSING_REPLY,
    RECORD_UNAVAILABLE_REPLY,
    ChatUnavailableError,
    canned_reply,
    converse,
    record_reply,
)
from app.intent_recognizer import MAX_QUESTION_CHARS, RecognizerUnavailableError, recognize_intent
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


# The local model unloads after a few idle minutes and a cold load takes about a minute, which
# is most of a chat turn's time budget. Load it at startup and keep it loaded.
OLLAMA_WARM_INTERVAL_SECONDS = 240
OLLAMA_KEEP_ALIVE = "30m"


async def _warm_ollama_once() -> bool:
    base = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
            # An empty prompt only loads the model; nothing is generated or sent from a patient.
            response = await client.post(
                f"{base}/api/generate", json={"model": model, "prompt": "", "keep_alive": OLLAMA_KEEP_ALIVE}
            )
        return response.is_success
    except httpx.HTTPError as error:
        logger.warning("Ollama warm-up failed (%s).", type(error).__name__)
        return False


async def _keep_ollama_warm() -> None:
    try:
        while True:
            await _warm_ollama_once()
            await asyncio.sleep(OLLAMA_WARM_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    application.state.supabase_client = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    warm_task = asyncio.create_task(_keep_ollama_warm())
    try:
        yield
    finally:
        warm_task.cancel()
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
    allow_methods=["GET", "POST", "DELETE"],
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
    _session_key: str | None = PrivateAttr(default=None)
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
    # Migration 015: a new correction first goes to the care team, so no Physician approval exists yet (None).
    approval_status: Literal["Pending", "Approved", "Rejected"] | None = None
    review_stage: Literal["with_care_team", "with_physician"] | None = None
    # Added by migration 014: whether a Technician coordination task was created ("unassigned" = none available;
    # the Physician review is unaffected). None when the database function predates 014.
    coordination: Literal["assigned", "unassigned"] | None = None


class PatientCorrectionCoordination(BaseModel):
    """What the Patient may see of the care team's coordination. Never notes, never who, never correction text."""

    task_id: int
    status: str
    status_label: str
    patient_message: str | None = None
    can_add_info: bool


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
    # Patient-safe stage: where the request is right now (migration 015 adds the care-team stages).
    review_stage: Literal["with_care_team", "needs_reply", "with_physician", "approved", "rejected", "declined"] | None = None
    # Set when the stored records are inconsistent; the request is then shown
    # as needing attention instead of being given an assumed status.
    status_issue: CorrectionStatusIssue | None = None
    coordination: PatientCorrectionCoordination | None = None


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
    login_session_id = auth_user.get("session_id")
    if isinstance(login_session_id, str):
        current_user._session_key = hashlib.sha256(
            f"{auth_user['auth_user_id']}:{login_session_id}".encode("utf-8")
        ).hexdigest()
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
    return await _load_own_record(user)


async def _load_own_record(user: CurrentUser) -> PatientMedicalRecordResponse:
    """The authenticated patient's own record. The user_id comes from the verified account,
    never from the client or a model. Shared by the REST endpoint and the chat assistant."""
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


def _review_stage(approval_status: str | None, coordination: Any) -> str | None:
    if approval_status == "Approved":
        return "approved"
    if approval_status == "Rejected":
        return "rejected"
    if approval_status == "Pending":
        return "with_physician"
    if coordination is None:
        return None
    if coordination.status == "Declined":
        return "declined"
    if coordination.status == "On Hold":
        return "needs_reply"
    return "with_care_team"


def _correction_history_item(
    request: dict[str, Any],
    approvals: list[dict[str, Any]],
    coordination: Any = None,
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
        # Technician-first (015): no approval is normal while the care team has the request. Without a coordination
        # task it is still an inconsistency and is shown as needing attention.
        if coordination is None:
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
        review_stage=None if issue is not None else _review_stage(approval_status, coordination),
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
    declined: set[int] = set()
    if any(not approvals.get(request_id) for request_id in request_ids):
        # Technician-first (015): a request with no approval is "closed" only if the care team declined it.
        # Fails closed: if the tasks cannot be read, nothing is submitted.
        task_rows = await _patient_service_rows(
            "technician_tasks",
            {
                "select": "task_id,status,approval_request_id",
                "requested_by": f"eq.{user_id}",
                "category": "eq.record_correction",
                "approval_request_id": f"in.({','.join(str(i) for i in request_ids)})",
            },
            unavailable_detail=CORRECTION_UNAVAILABLE_DETAIL,
        )
        declined = {
            r["approval_request_id"] for r in task_rows
            if r.get("status") in ("Declined", "Cancelled") and isinstance(r.get("approval_request_id"), int)
        }
    for request_id in request_ids:
        request_approvals = approvals.get(request_id, [])
        if not request_approvals and request_id in declined:
            continue
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
    coordination = await _correction_coordination(user.user_id, [row["request_id"] for row in requests])
    items = []
    for row in requests:
        item = _correction_history_item(row, approvals.get(row["request_id"], []), coordination.get(row["request_id"]))
        item.coordination = coordination.get(row["request_id"])
        items.append(item)
    return PatientCorrectionHistoryResponse(items=items)


async def _correction_coordination(user_id: int, request_ids: list[int]) -> dict[int, PatientCorrectionCoordination]:
    """Best effort and read-only: corrections keep working (without this extra) if it cannot be loaded."""
    try:
        from app.technician.features import service_requests_enabled

        if not service_requests_enabled() or not request_ids:
            return {}
        from app.service_requests import service as service_requests  # late import: it imports this module

        return await service_requests.correction_coordination(user_id, request_ids)
    except Exception:  # noqa: BLE001 - the correction history itself must never fail because of the extra
        logger.warning("Correction coordination status is unavailable; showing corrections without it.")
        return {}


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

    return await _submit_correction(user, body["proposed_treatment"])


async def _submit_correction(
    user: CurrentUser, proposed_treatment: str
) -> PatientCorrectionRequestResponse:
    """The single correction write pathway (direct form and assistant confirm both use it)."""
    if user.role != Role.PATIENT or "submit_record_correction" not in user.permissions:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to submit correction requests.",
        )
    if (
        not proposed_treatment.strip()
        or len(proposed_treatment) > 2000
    ):
        raise HTTPException(
            status_code=422,
            detail="The proposed treatment must contain 1 to 2000 characters.",
        )

    # A patient may have any number of correction requests open at once (demo requirement);
    # there is deliberately no one-pending-request pre-check here.
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
        error_reason = ""
        try:
            rpc_error = response.json()
            if isinstance(rpc_error, dict):
                code = rpc_error.get("code")
                message = rpc_error.get("message")
                reason = rpc_error.get("details")
                if isinstance(code, str):
                    error_code = code
                if isinstance(message, str):
                    error_message = message
                if isinstance(reason, str):
                    error_reason = reason
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
        if error_code == "PT422" and error_reason == "no_eligible_technician":
            # Technician-first (015): fail closed. Never routed to the Physician, nothing was written.
            await _insert_chat_audit(user, "patient.correction.not_accepted", {"reason": "no_eligible_technician"})
            raise HTTPException(
                status_code=409,
                detail="No technician is currently available for your care team, so your correction request "
                       "could not be sent. Nothing was submitted. Please contact your care team.",
            )
        if error_code == "PT409":
            # Raised by the database function (migration 007) when a correction
            # request for this Patient is already awaiting review.
            raise HTTPException(
                status_code=409,
                detail=(
                    "A correction request is already awaiting review. "
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
        # 015: no approval yet (None) while the care team reviews; a pre-015 database function still says Pending.
        or result.get("approval_status") not in (None, "Pending")
        or (result.get("approval_status") is None and result.get("coordination") != "assigned")
    ):
        logger.error("Patient correction RPC returned an unexpected response shape.")
        raise HTTPException(
            status_code=502,
            detail=CORRECTION_UNCONFIRMED_DETAIL,
        )

    return PatientCorrectionRequestResponse(
        request_id=result["request_id"],
        guardrail_decision="Escalate",
        approval_status=result.get("approval_status"),
        review_stage="with_physician" if result.get("approval_status") == "Pending" else "with_care_team",
        coordination=result.get("coordination") if result.get("coordination") in ("assigned", "unassigned") else None,
    )


class PatientIntentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class PatientIntentResponse(BaseModel):
    intent: str
    decision: Literal["Allow", "Block", "Escalate"]
    reason_code: str


@app.post("/api/patient/assistant/intent", response_model=PatientIntentResponse)
async def classify_patient_intent(
    payload: PatientIntentRequest,
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> PatientIntentResponse:
    """Dry run: the model proposes an intent; the gateway decides. Nothing is executed or stored."""
    if ASSISTANT_PERMISSION not in user.permissions:
        raise HTTPException(status_code=403, detail="You do not have permission to use the assistant.")
    try:
        recognition = await recognize_intent(payload.question)
    except RecognizerUnavailableError as error:
        raise HTTPException(status_code=503, detail="The assistant is unavailable right now.") from error
    result = decide(
        role=user.role.value,
        permissions=user.permissions,
        proposal=parse_proposal(recognition.text),
        rails_passed=recognition.rails_passed,
    )
    return PatientIntentResponse(intent=result.intent, decision=result.decision, reason_code=result.reason_code)


class PatientChatRequest(BaseModel):
    model_config = {"extra": "forbid"}
    message: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    state_token: str | None = Field(default=None, max_length=chat_state.MAX_TOKEN_CHARS)
    # Optional chat-history fields (ignored unless HASF_CHAT_HISTORY_ENABLED=true).
    chat_id: UUID | None = None
    turn_id: UUID | None = None
    is_new_chat: bool = False


class PatientChatResponse(BaseModel):
    reply: str
    decision: Literal["Allow", "Block", "Escalate"]
    intent: str
    mode: Literal[
        "answer", "refusal", "notice", "pointer", "greeting", "offer", "details_step", "cancelled",
        "record",
    ]
    state_token: str | None = None
    # Explainable decision: a stable code plus a plain-language reason (never message text).
    reason_code: str | None = None
    reason: str | None = None
    # What happened to the patient's own data: none | retrieved | not_found | unavailable.
    data_access: Literal["none", "retrieved", "not_found", "unavailable"] | None = None
    # Added fields; existing clients read only reply/mode/state_token and ignore these.
    chat_id: UUID | None = None
    turn_id: UUID | None = None
    save_status: Literal["saved", "duplicate", "not_saved", "unconfirmed", "disabled"] | None = None
    save_reason: (
        Literal[
            "no_ids", "filtered", "not_eligible", "sensitive", "refused", "redacted",
            "chat_unavailable", "chat_full", "chat_limit", "error",
        ]
        | None
    ) = None


# Shorter than the browser's chat wait (ASSISTANT_CHAT_TIMEOUT_MS = 100 s in patientApi.js).
CHAT_TURN_BUDGET_SECONDS = 85

CHAT_STATE_INVALID_DETAIL = "This conversation step is no longer valid. Please start again."
CHAT_OFFER_REPLY = (
    "I can't submit corrections from this chat, but I can help you raise a correction request. "
    "Your care team checks it first, and your assigned Physician reviews it if clinical authorization is needed. "
    "Would you like to raise a correction request?"
)
CHAT_DETAILS_REPLY = (
    "Okay. Please enter the proposed correction in the secure request-details box, not in this chat. "
    "You'll see a review step and must confirm explicitly before anything is submitted. "
    "Nothing has been submitted yet."
)
CHAT_DETAILS_WAITING_REPLY = (
    "Please use the secure request-details box for the correction, or say cancel. "
    "Nothing has been submitted."
)
CHAT_CANCELLED_REPLY = "Okay, I won't raise a correction request. Nothing was submitted."
CHAT_NOTHING_PENDING_REPLY = "There's nothing waiting for a yes or no right now. What would you like to know?"


def _chat_notice(reply: str, mode: str, intent: str, token: str | None = None) -> "PatientChatResponse":
    return PatientChatResponse(reply=reply, decision="Escalate", intent=intent, mode=mode, state_token=token)


async def _correction_offer_or_pending(user: CurrentUser) -> "PatientChatResponse":
    """Offer help with a correction request (several may be open at once; no pending-request block)."""
    return _chat_notice(
        CHAT_OFFER_REPLY, "offer", "request_correction",
        chat_state.issue_state(user.user_id, chat_state.STEP_OFFERED, session_key=user._session_key or ""),
    )


CHAT_CONFIRM_REPLY = (
    "A chat reply can't submit a correction. To submit it, press Confirm on the review card, "
    "or say cancel. Nothing has been submitted."
)


async def _chat_follow_up(payload: "PatientChatRequest", user: CurrentUser) -> "PatientChatResponse | None":
    """Handle a message that carries a state token. None means: continue as a fresh message."""
    session_key = user._session_key or ""
    try:
        step = chat_state.peek_state(payload.state_token, user.user_id, session_key=session_key)
        reply = chat_state.interpret_reply(payload.message)
        if step == chat_state.STEP_CONFIRM:
            # Only the Confirm button (separate endpoint) can submit; chat can only cancel.
            if reply in {"no", "cancel"}:
                chat_state.consume_state(
                    payload.state_token, user.user_id, session_key=session_key, ignore_hash=True
                )
                return _chat_notice(CHAT_CANCELLED_REPLY, "cancelled", "request_correction")
            return _chat_notice(CHAT_CONFIRM_REPLY, "notice", "request_correction")
        chat_state.consume_state(payload.state_token, user.user_id, session_key=session_key)
    except chat_state.InvalidStateError as error:
        raise HTTPException(status_code=409, detail=CHAT_STATE_INVALID_DETAIL) from error
    if reply in {"no", "cancel"}:
        return _chat_notice(CHAT_CANCELLED_REPLY, "cancelled", "request_correction")
    if step == chat_state.STEP_DETAILS:
        # Free text is never forwarded to a model while details are pending.
        return _chat_notice(
            CHAT_DETAILS_WAITING_REPLY, "details_step", "request_correction",
            chat_state.issue_state(user.user_id, chat_state.STEP_DETAILS, session_key=session_key),
        )
    if reply == "yes":
        # Re-check authorization and the Pending restriction; "yes" itself authorizes nothing.
        if "submit_record_correction" not in user.permissions:
            raise HTTPException(status_code=403, detail="You do not have permission to submit corrections.")
        offer = await _correction_offer_or_pending(user)
        if offer.mode != "offer":
            return offer
        return _chat_notice(
            CHAT_DETAILS_REPLY, "details_step", "request_correction",
            chat_state.issue_state(user.user_id, chat_state.STEP_DETAILS, session_key=session_key),
        )
    return None


class CorrectionStepRequest(BaseModel):
    """Structured, authenticated field for the proposal. Never routed through chat or a model."""

    model_config = {"extra": "forbid"}
    proposed_treatment: str = Field(min_length=1, max_length=2000)
    state_token: str = Field(min_length=1, max_length=chat_state.MAX_TOKEN_CHARS)


class CorrectionReviewResponse(BaseModel):
    proposed_treatment: str
    state_token: str
    expires_in_seconds: int
    notice: str


def _assistant_correction_guard(user: CurrentUser, payload: CorrectionStepRequest) -> str:
    if (
        user.role != Role.PATIENT
        or ASSISTANT_PERMISSION not in user.permissions
        or "submit_record_correction" not in user.permissions
    ):
        raise HTTPException(status_code=403, detail="You do not have permission to submit correction requests.")
    if not payload.proposed_treatment.strip():
        raise HTTPException(status_code=422, detail="The proposed treatment must contain 1 to 2000 characters.")
    if not user._session_key:
        raise HTTPException(status_code=409, detail="Please sign in again to continue.")
    return user._session_key


@app.post("/api/patient/assistant/correction/review", response_model=CorrectionReviewResponse)
async def review_assistant_correction(
    payload: CorrectionStepRequest,
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> CorrectionReviewResponse:
    """Review step: needs the details-step token. Nothing is written or sent to a model."""
    session_key = _assistant_correction_guard(user, payload)
    try:
        step = chat_state.consume_state(payload.state_token, user.user_id, session_key=session_key)
    except chat_state.InvalidStateError as error:
        raise HTTPException(status_code=409, detail=CHAT_STATE_INVALID_DETAIL) from error
    if step != chat_state.STEP_DETAILS:
        raise HTTPException(status_code=409, detail=CHAT_STATE_INVALID_DETAIL)
    return CorrectionReviewResponse(
        proposed_treatment=payload.proposed_treatment,
        state_token=chat_state.issue_state(
            user.user_id, chat_state.STEP_CONFIRM, session_key=session_key,
            proposal_hash=chat_state.bind_text(payload.proposed_treatment),
        ),
        expires_in_seconds=chat_state.STATE_TTL_SECONDS,
        notice=(
            "This does not change your record. Your care team checks it first, and your assigned Physician "
            "reviews it if clinical authorization is needed. "
            "Press Confirm to submit this request."
        ),
    )


@app.post(
    "/api/patient/assistant/correction/confirm",
    response_model=PatientCorrectionRequestResponse,
)
async def confirm_assistant_correction(
    payload: CorrectionStepRequest,
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> PatientCorrectionRequestResponse:
    """Final Confirm: a valid, single-use confirm token for this exact text, then the shared submit path.

    The token is consumed before submitting and the submission is never retried.
    """
    session_key = _assistant_correction_guard(user, payload)
    try:
        step = chat_state.consume_state(
            payload.state_token, user.user_id, session_key=session_key,
            proposal_hash=chat_state.bind_text(payload.proposed_treatment),
        )
    except chat_state.InvalidStateError as error:
        raise HTTPException(status_code=409, detail=CHAT_STATE_INVALID_DETAIL) from error
    if step != chat_state.STEP_CONFIRM:
        raise HTTPException(status_code=409, detail=CHAT_STATE_INVALID_DETAIL)
    return await _submit_correction(user, payload.proposed_treatment)


def _fallback_reason_code(response: PatientChatResponse) -> str:
    if response.intent == "greeting":
        return "greeting"
    if response.intent == "none":
        return "no_pending_step"
    if response.decision == "Allow":
        return "low_risk_permitted"
    if response.decision == "Escalate":
        return "high_risk_requires_physician"
    return "guardrail_blocked"


async def _write_chat_audit(user: CurrentUser, response: PatientChatResponse) -> None:
    """One audit_logs row per chat decision. Never contains message or reply text.

    A failed audit write is logged but never changes or blocks the patient's reply.
    """
    await _insert_chat_audit(
        user,
        f"patient.chat.{response.decision.lower()}",
        {
            "intent": response.intent,
            "reason_code": response.reason_code,
            "reason": response.reason,
            "mode": response.mode,
            "data_access": response.data_access,
        },
    )


async def _insert_chat_audit(user: CurrentUser, event_type: str, details: dict[str, Any]) -> None:
    try:
        key = _service_role_key()
        audit = await _request_supabase(
            "/rest/v1/audit_logs",
            api_key=key,
            bearer_token=key,
            method="POST",
            json_body={
                "user_id": user.user_id,
                "event_type": event_type,
                "details": json.dumps(details, separators=(",", ":")),
            },
            extra_headers={"Prefer": "return=minimal"},
        )
        if not audit.is_success:
            logger.error("Patient chat audit insert returned HTTP %s.", audit.status_code)
    except Exception as error:  # never let auditing break the reply
        logger.error("Patient chat audit insert failed (%s).", type(error).__name__)


async def _run_chat_turn(payload: PatientChatRequest, user: CurrentUser) -> PatientChatResponse:
    """One assistant turn with an explainable reason and an audit event for its decision."""
    response = await _run_chat_turn_inner(payload, user)
    if response.decision == "Allow" and response.mode == "refusal":
        # Safety net: a permitted request whose answer was stopped is never reported as Allow.
        response.decision = "Block"
        response.reason_code = "response_blocked_by_safety_check"
        response.reason = None
    if response.data_access is None:
        response.data_access = "none"
    if response.reason_code is None:
        response.reason_code = _fallback_reason_code(response)
    if response.reason is None:
        response.reason = explain(response.reason_code)
    await _write_chat_audit(user, response)
    return response


async def _own_record_reply(message: str, user: CurrentUser) -> PatientChatResponse:
    """Answer from the authenticated patient's own record only. No model is involved.

    Authorization still goes through the gateway. The lookup uses the verified account's
    user_id (never the message), and a failure is reported as such rather than guessed at.
    """
    gateway = decide(
        role=user.role.value,
        permissions=user.permissions,
        proposal=IntentProposal("view_own_record", 1.0),
        rails_passed=True,
    )
    if gateway.decision != "Allow":
        chat = await converse(message, gateway)
        return PatientChatResponse(
            reply=chat.reply, decision=gateway.decision, intent=gateway.intent,
            mode=chat.mode, reason_code=gateway.reason_code,  # type: ignore[arg-type]
        )
    try:
        record = (await _load_own_record(user)).record
    except HTTPException:
        return PatientChatResponse(
            reply=RECORD_UNAVAILABLE_REPLY, decision="Allow", intent="view_own_record", mode="notice",
            reason_code="record_unavailable", data_access="unavailable",
        )
    if record is None:
        return PatientChatResponse(
            reply=RECORD_MISSING_REPLY, decision="Allow", intent="view_own_record", mode="notice",
            reason_code="record_not_found", data_access="not_found",
        )
    return PatientChatResponse(
        reply=record_reply(
            message, age=record.age, medical_condition=record.medical_condition, treatment=record.treatment
        ),
        decision="Allow", intent="view_own_record", mode="record",
        reason_code="record_retrieved", data_access="retrieved",
    )


async def _run_chat_turn_inner(payload: PatientChatRequest, user: CurrentUser) -> PatientChatResponse:
    """The assistant turn (follow-up, greeting, safety screen, rails, gateway, reply)."""
    if payload.state_token is not None:
        follow_up = await _chat_follow_up(payload, user)
        if follow_up is not None:
            return follow_up
    elif chat_state.interpret_reply(payload.message) is not None:
        return PatientChatResponse(
            reply=CHAT_NOTHING_PENDING_REPLY, decision="Allow", intent="none", mode="notice"
        )
    greeting = canned_reply(payload.message)
    if greeting is not None:  # fixed, non-actionable reply; no model call
        return PatientChatResponse(reply=greeting.reply, decision="Allow", intent="greeting", mode="greeting")
    # Deterministic: emergencies and forbidden requests (including another person's private information)
    # never reach a model or a record lookup. The patient's own name only exempts their own name.
    verdict = safety_rules.screen(payload.message, own_name=user.name)
    if verdict is not None:
        return PatientChatResponse(
            reply=verdict.reply,
            decision=verdict.decision,
            intent=verdict.intent,
            mode=verdict.mode,
            reason_code=verdict.reason_code,
        )
    if safety_rules.is_own_record_query(payload.message):  # a lookup of the patient's own details
        return await _own_record_reply(payload.message, user)
    try:
        # One budget for the whole turn (intent, answer and any retry), shorter than the browser's
        # wait, so the patient gets a clear "unavailable" instead of the browser giving up first.
        async with asyncio.timeout(CHAT_TURN_BUDGET_SECONDS):
            recognition = await recognize_intent(payload.message)
            gateway = decide(
                role=user.role.value,
                permissions=user.permissions,
                proposal=parse_proposal(recognition.text),
                rails_passed=recognition.rails_passed,
            )
            if gateway.decision == "Escalate" and gateway.intent == "request_correction":
                return await _correction_offer_or_pending(user)
            if (
                gateway.decision == "Allow"
                and gateway.intent == "view_own_record"
                and not safety_rules.is_navigation_or_third_party(payload.message)
            ):
                return await _own_record_reply(payload.message, user)
            chat = await converse(payload.message, gateway)
    except (RecognizerUnavailableError, ChatUnavailableError, TimeoutError) as error:
        logger.warning("Chat turn unavailable (%s).", type(error).__name__)
        await _insert_chat_audit(
            user, "patient.chat.unavailable", {"reason_code": "assistant_unavailable", "cause": type(error).__name__}
        )
        raise HTTPException(status_code=503, detail="The assistant is unavailable right now.") from error
    decision, reason_code = gateway.decision, gateway.reason_code
    if chat.mode == "refusal" and decision == "Allow":
        # Permitted, but the generated answer was stopped. Say which component stopped it.
        decision, reason_code = "Block", _blocked_reason_code(chat.blocked_by, chat.blocked_detail)
    return PatientChatResponse(
        reply=chat.reply,
        decision=decision,
        intent=gateway.intent,
        mode=chat.mode,
        reason_code=reason_code,
    )


def _blocked_reason_code(blocked_by: str | None, detail: str | None) -> str:
    """Safe reason code for a stopped answer: names the component (and category), never any text."""
    if blocked_by == "input_rail":
        return "response_blocked_by_input_rail"
    if blocked_by == "output_rail":
        return "response_blocked_by_output_rail"
    if blocked_by == "output_check":
        return f"response_blocked_by_output_check_{detail}" if detail else "response_blocked_by_output_check"
    return "response_blocked_by_safety_check"


# ---- Persistent chat history (migration 008). Everything below is inert unless
# HASF_CHAT_HISTORY_ENABLED=true; deletion alone is always available to the owner. ----

CHAT_HISTORY_UNAVAILABLE_DETAIL = "Chat history is temporarily unavailable."
CHAT_REPLAY_INTENT = "replay"


async def _chat_history_rpc(function: str, body: dict[str, Any]) -> Any:
    """POST one of the six migration-008 functions with the service role. Returns parsed JSON."""
    try:
        service_key = _service_role_key()
        response = await _request_supabase(
            f"/rest/v1/rpc/{function}",
            api_key=service_key,
            method="POST",
            json_body=body,
            extra_headers={"Authorization": f"Bearer {service_key}"},
        )
    except (HTTPException, httpx.HTTPError) as error:
        transport_error = error.__cause__ if isinstance(error, HTTPException) else error
        unconfirmed = isinstance(transport_error, httpx.RequestError) and not isinstance(
            transport_error, CORRECTION_NOT_SENT_ERRORS
        )
        logger.error("Chat history RPC %s failed (%s).", function, type(transport_error or error).__name__)
        raise chat_history.ChatHistoryRpcError(unconfirmed=unconfirmed) from error
    if not response.is_success:
        error_code = ""
        try:
            rpc_error = response.json()
            if isinstance(rpc_error, dict) and isinstance(rpc_error.get("code"), str):
                error_code = rpc_error["code"]
        except ValueError:
            pass
        logger.error(
            "Chat history RPC %s returned HTTP %s (database code %s).",
            function, response.status_code, error_code or "unavailable",
        )
        raise chat_history.ChatHistoryRpcError(
            error_code,
            unconfirmed=chat_history.http_failure_unconfirmed(response.status_code, error_code),
            status=response.status_code,
        )
    try:
        return response.json()
    except ValueError as error:
        raise chat_history.ChatHistoryRpcError(unconfirmed=True) from error


def _with_save(
    response: PatientChatResponse,
    payload: PatientChatRequest,
    status: str,
    reason: str | None = None,
) -> PatientChatResponse:
    response.chat_id = payload.chat_id
    response.turn_id = payload.turn_id
    response.save_status = status  # type: ignore[assignment]
    response.save_reason = reason  # type: ignore[assignment]
    return response


def _replayed_response(
    stored: dict[str, Any], payload: PatientChatRequest
) -> PatientChatResponse | None:
    """Rebuild a stored turn. Correction events never come back as a resumable step."""
    reply, mode = stored.get("reply"), stored.get("mode")
    if not isinstance(reply, str) or not isinstance(mode, str):
        return None
    if mode == chat_history.EVENT_MODE:
        reply, mode = chat_history.REPLAY_EVENT_NOTICE, "notice"
    elif mode not in chat_history.SAVEABLE_MODES:
        return None
    return _with_save(
        PatientChatResponse(
            reply=reply, decision="Allow", intent=CHAT_REPLAY_INTENT, mode=mode  # type: ignore[arg-type]
        ),
        payload,
        "duplicate",
    )


async def _find_stored_turn(user: CurrentUser, payload: PatientChatRequest) -> dict[str, Any] | None:
    """Replay check, before any model call or token use. Raises 422 on turn-id misuse."""
    for text in chat_history.find_candidates(payload.message, payload.state_token):
        try:
            found = await _chat_history_rpc(
                "find_patient_chat_turn",
                {
                    "p_user_id": user.user_id,
                    "p_chat_id": str(payload.chat_id),
                    "p_turn_id": str(payload.turn_id),
                    "p_user_text": text,
                },
            )
        except chat_history.ChatHistoryRpcError as error:
            if error.db_code == "PT422":
                continue  # try the other candidate text; 422 only if none matches
            raise
        if isinstance(found, dict) and found.get("found") is True:
            return found
        if isinstance(found, dict) and found.get("found") is False:
            return None
        raise chat_history.ChatHistoryRpcError(unconfirmed=False)  # unreadable answer
    raise HTTPException(status_code=422, detail="This turn id was already used for a different message.")


async def _save_turn(
    user: CurrentUser, payload: PatientChatRequest, response: PatientChatResponse
) -> PatientChatResponse:
    plan, skip_reason = chat_history.evaluate_persistence(
        message=payload.message,
        reply=response.reply,
        mode=response.mode,
        intent=response.intent,
        state_token=payload.state_token,
    )
    if plan is None:
        return _with_save(response, payload, "not_saved", skip_reason)
    try:
        result = await _chat_history_rpc(
            "save_patient_chat_turn",
            {
                "p_user_id": user.user_id,
                "p_chat_id": str(payload.chat_id),
                "p_is_new": payload.is_new_chat,
                "p_turn_id": str(payload.turn_id),
                "p_user_text": plan.user_text,
                "p_reply_text": plan.reply_text,
                "p_reply_mode": plan.reply_mode,
            },
        )
    except chat_history.ChatHistoryRpcError as error:
        if error.unconfirmed:
            return _with_save(response, payload, "unconfirmed")
        reason = {"PT404": "chat_unavailable", "PT409": "chat_full", "PT429": "chat_limit"}.get(
            error.db_code, "error"
        )
        return _with_save(response, payload, "not_saved", reason)
    if not isinstance(result, dict) or not isinstance(result.get("duplicate"), bool):
        # A 2xx with an unreadable body: the write may have committed.
        return _with_save(response, payload, "unconfirmed")
    if result["duplicate"]:
        # Another request saved this turn first; answer consistently with what is stored.
        replay = _replayed_response(result, payload)
        return replay if replay is not None else _with_save(response, payload, "duplicate")
    # A correction step is kept only as a fixed placeholder; say so instead of "conversation saved".
    redacted = "redacted" if plan.reply_mode == chat_history.EVENT_MODE else None
    return _with_save(response, payload, "saved", redacted)


_SAVE_FIELDS = {"chat_id", "turn_id", "save_status", "save_reason"}


def _chat_json(response: PatientChatResponse, include_save: bool) -> JSONResponse:
    """Serialize a chat reply. Without chat-history ids the body keeps its original shape,
    so existing clients with strict response validation see no new fields."""
    return JSONResponse(response.model_dump(mode="json", exclude=None if include_save else _SAVE_FIELDS))


@app.post("/api/patient/assistant/chat", response_model=PatientChatResponse)
async def chat_with_patient_assistant(
    payload: PatientChatRequest,
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> Response:
    """Conversational reply. The Action Gateway decides; nothing is executed.

    With HASF_CHAT_HISTORY_ENABLED=true and chat_id/turn_id supplied, safe turns are also saved.
    """
    if ASSISTANT_PERMISSION not in user.permissions:
        raise HTTPException(status_code=403, detail="You do not have permission to use the assistant.")
    opted_in = payload.chat_id is not None or payload.turn_id is not None
    if not chat_history.enabled():
        return _chat_json(
            _with_save(await _run_chat_turn(payload, user), payload, "disabled"), include_save=opted_in
        )
    if payload.chat_id is None or payload.turn_id is None:
        return _chat_json(
            _with_save(await _run_chat_turn(payload, user), payload, "not_saved", "no_ids"),
            include_save=True,
        )
    return _chat_json(await _persisted_chat_turn(payload, user), include_save=True)


async def _persisted_chat_turn(payload: PatientChatRequest, user: CurrentUser) -> PatientChatResponse:
    assert payload.turn_id is not None
    async with chat_history.inflight(user.user_id, payload.turn_id):
        try:
            stored = await _find_stored_turn(user, payload)
        except chat_history.ChatHistoryRpcError:
            # Cannot check for a replay, so do not risk a double save: answer without saving.
            return _with_save(await _run_chat_turn(payload, user), payload, "not_saved", "error")
        if stored is not None:
            replay = _replayed_response(stored, payload)
            if replay is not None:
                return replay
            return _with_save(await _run_chat_turn(payload, user), payload, "not_saved", "error")
        return await _save_turn(user, payload, await _run_chat_turn(payload, user))


class PatientChatSummary(BaseModel):
    model_config = {"extra": "forbid"}
    chat_id: UUID
    title: str
    last_activity_at: str
    message_count: int


class PatientChatListResponse(BaseModel):
    chats: list[PatientChatSummary]


class PatientChatMessage(BaseModel):
    """Stored text only. There is deliberately no state_token: correction steps never resume."""

    model_config = {"extra": "forbid"}
    role: Literal["user", "assistant"]
    content: str
    display_mode: str | None = None
    created_at: str


class PatientChatMessagesResponse(BaseModel):
    chat_id: UUID
    messages: list[PatientChatMessage]


def _history_error(error: "chat_history.ChatHistoryRpcError") -> HTTPException:
    if error.db_code == "PT404":
        return HTTPException(status_code=404, detail="Conversation not found.")
    if error.unconfirmed:
        return HTTPException(status_code=504, detail=CHAT_HISTORY_UNAVAILABLE_DETAIL)
    return HTTPException(status_code=503, detail=CHAT_HISTORY_UNAVAILABLE_DETAIL)


def _require_history_enabled() -> None:
    if not chat_history.enabled():
        raise HTTPException(status_code=404, detail="Not found.")


def _rows(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("unexpected chat history response")
    return value


@app.get("/api/patient/assistant/chats", response_model=PatientChatListResponse)
async def list_patient_chat_history(
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> PatientChatListResponse:
    _require_history_enabled()
    try:
        rows = await _chat_history_rpc("list_patient_chats", {"p_user_id": user.user_id})
        return PatientChatListResponse(chats=_rows(rows))  # type: ignore[arg-type]
    except chat_history.ChatHistoryRpcError as error:
        raise _history_error(error) from error
    except ValueError as error:  # includes pydantic ValidationError
        raise HTTPException(status_code=503, detail=CHAT_HISTORY_UNAVAILABLE_DETAIL) from error


@app.get("/api/patient/assistant/chats/{chat_id}", response_model=PatientChatMessagesResponse)
async def get_patient_chat_history(
    chat_id: UUID,
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> PatientChatMessagesResponse:
    _require_history_enabled()
    try:
        rows = await _chat_history_rpc(
            "get_patient_chat_messages", {"p_user_id": user.user_id, "p_chat_id": str(chat_id)}
        )
        # Stored correction-step placeholders become one privacy notice; the stored rows are untouched.
        shown = chat_history.present_messages(_rows(rows))
        return PatientChatMessagesResponse(chat_id=chat_id, messages=shown)  # type: ignore[arg-type]
    except chat_history.ChatHistoryRpcError as error:
        raise _history_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=503, detail=CHAT_HISTORY_UNAVAILABLE_DETAIL) from error


@app.delete("/api/patient/assistant/chats/{chat_id}", status_code=204)
async def delete_patient_chat_history(
    chat_id: UUID,
    user: Annotated[CurrentUser, Depends(require_patient)],
) -> Response:
    """Owner-only delete; available even when persistence is disabled."""
    try:
        deleted = await _chat_history_rpc(
            "delete_patient_chat", {"p_user_id": user.user_id, "p_chat_id": str(chat_id)}
        )
    except chat_history.ChatHistoryRpcError as error:
        raise _history_error(error) from error
    if deleted is not True:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return Response(status_code=204)


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


# Technician portal routes. Imported last so the technician package can use
# the dependencies defined above without a circular-import problem.
from app.technician.errors import register_error_handlers  # noqa: E402
from app.technician.routes import router as technician_router  # noqa: E402
from app.service_requests.routes import router as service_requests_router  # noqa: E402
from app.scheduling.routes import router as appointments_router  # noqa: E402
from app.scheduling.physician_routes import router as physician_schedule_router  # noqa: E402

register_error_handlers(app)
app.include_router(technician_router)
app.include_router(service_requests_router)
app.include_router(appointments_router)
app.include_router(physician_schedule_router)
