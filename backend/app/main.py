import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator, Awaitable, Callable, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field

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

    return {"auth_user_id": str(auth_user_id), "email": email}


async def _load_linked_user(auth_user: dict[str, str]) -> tuple[int, str]:
    response = await _request_supabase(
        "/rest/v1/users",
        api_key=_service_role_key(),
        bearer_token=_service_role_key(),
        params={
            "select": "user_id,role,is_active",
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
        Role(database_role.casefold())
        is_active = rows[0]["is_active"]
        if not isinstance(is_active, bool):
            raise ValueError("Application account active status must be boolean.")
    except (KeyError, TypeError, ValueError) as error:
        logger.error("The linked application account has an invalid id, role, or active status.")
        raise HTTPException(status_code=403, detail="The linked application account is invalid.") from error

    if not is_active:
        raise HTTPException(status_code=403, detail="This application account has been deactivated.")

    return user_id, database_role


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
    (user_id, database_role), permissions_by_role = await asyncio.gather(
        _load_linked_user(auth_user),
        _load_permissions(),
    )
    permissions = permissions_by_role.get(database_role.casefold(), [])

    return CurrentUser(
        user_id=user_id,
        email=auth_user["email"],
        role=Role(database_role.casefold()),
        permissions=permissions,
    )


def require_permission(action: str):
    async def check_permission(
        user: Annotated[CurrentUser, Depends(get_current_user)],
    ) -> CurrentUser:
        if action not in user.permissions:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
        return user

    return check_permission


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


@app.get("/api/authorization/{action}", response_model=AuthorizationResponse)
async def check_action_permission(
    action: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> AuthorizationResponse:
    allowed = action in user.permissions
    return AuthorizationResponse(action=action, allowed=allowed)


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
    user: Annotated[CurrentUser, Depends(get_current_user)],
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
