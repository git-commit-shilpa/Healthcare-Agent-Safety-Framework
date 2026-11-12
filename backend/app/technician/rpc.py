"""Calls the task RPCs through PostgREST with the service-role key and maps errors safely.

Mirrors the existing correction RPC handling in app.main: transport failures before the
request was sent are 503, failures that may have happened after the database committed
are 504 ("could not confirm"), and database errors become a fixed, safe message. Database
messages, details and hints are logged by code only and are never returned to the client.
"""

import logging
from typing import Any

import httpx
from fastapi import HTTPException

import app.main as main
from app.technician.errors import TaskApiError

logger = logging.getLogger(__name__)

UNAVAILABLE = "Service requests are temporarily unavailable."
UNCONFIRMED = (
    "We could not confirm whether that action completed. "
    "Please check the request's current status before trying again."
)

# detail reason (set by the SQL functions) -> (HTTP status, safe message)
_BY_REASON: dict[str, tuple[int, str]] = {
    "not_found": (404, "Not found."),
    "invalid_state": (409, "That action is not available for the request in its current state."),
    "approval_required": (409, "Physician approval is required before this request can proceed."),
    "approval_pending": (409, "A physician approval is still pending for this request."),
    "approval_approved": (409, "Physician approval has already been granted for this request."),
    "approval_rejected": (409, "The Physician rejected this request; it can only be declined."),
    "approval_invalid": (409, "The linked approval is not valid for this request."),
    "no_unique_reviewer": (409, "An authorized Physician is unavailable to review this request."),
    "duplicate_request": (409, "An identical request is already open."),
    "too_many_open": (429, "You have too many open requests. Please wait for some to be completed."),
    "no_eligible_technician": (
        422,
        "No technician is currently available for your care team. Please contact your care team.",
    ),
    "note_required": (422, "An internal reason and a patient-facing message are required."),
    "not_patient": (403, "You do not have permission to perform this action."),
    "not_technician": (403, "You do not have permission to perform this action."),
    "permission_missing": (403, "You do not have permission to perform this action."),
    "patient_link": (403, "You do not have permission to perform this action."),
    # Appointment scheduling (migration 013)
    "slot_unavailable": (409, "That time is no longer available. Please choose another time."),
    "patient_overlap": (409, "You already have an appointment request at that time."),
    "invalid_date": (422, "Please choose a valid date and time."),
    "physician_unavailable": (422, "That doctor is not available for appointments."),
    "confirmation_message_required": (422, "A confirmation message for the patient is required."),
    "rejection_reason_too_short": (422, "Please explain the rejection to the patient (at least 20 characters)."),
    "information_request_too_short": (
        422, "Please tell the patient what information is needed (at least 10 characters)."),
    "invalid_timezone": (422, "That time zone is not recognised."),
    "timezone_mismatch": (422, "All schedule rules must use the same time zone."),
    "rule_overlap": (409, "That working window overlaps another one."),
    "not_physician": (403, "You do not have permission to perform this action."),
    # Record-correction coordination (migration 014)
    "correction_coordination_only": (
        409, "Correction requests are coordinated by the care team and decided by the Physician."),
    "not_a_correction_task": (409, "This is not a correction request."),
    "already_decided": (409, "The Physician has already decided this correction."),
    "invalid_action": (422, "That action is not recognised."),
    "invalid_correction_link": (422, "The correction link is not valid."),
    # Technician-first corrections (migration 015)
    "physician_unreachable": (
        409, "The assigned Physician cannot receive approval requests yet (no linked account). Ask an administrator."),
    "cannot_decline_after_authorization": (409, "This correction is already with the Physician, so it cannot be declined."),
    "follow_up_required": (409, "Ask the patient for the missing information before declining for that reason."),
    "approval_requires_technician_escalation": (409, "A Physician approval can only be requested by the assigned Technician."),
}
_BY_CODE: dict[str, tuple[int, str, str]] = {
    "PT403": (403, "You do not have permission to perform this action.", "forbidden"),
    "PT404": (404, "Not found.", "not_found"),
    "PT409": (409, "That action is not available right now.", "conflict"),
    "PT422": (422, "The request is invalid.", "invalid_request"),
    "PT429": (429, "Too many requests. Please try again later.", "too_many_requests"),
    "42501": (403, "You do not have permission to perform this action.", "forbidden"),
}


def map_rpc_error(response: httpx.Response) -> TaskApiError:
    code = reason = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            code = body.get("code") if isinstance(body.get("code"), str) else ""
            reason = body.get("details") if isinstance(body.get("details"), str) else ""
    except ValueError:
        pass
    logger.error(
        "Task RPC returned HTTP %s (database code %s, reason %s).",
        response.status_code, code or "none", reason or "none",
    )
    if not code and response.status_code in main.CORRECTION_UNCONFIRMED_GATEWAY_STATUSES:
        return TaskApiError(504, UNCONFIRMED, "unconfirmed")
    if reason in _BY_REASON and code in _BY_CODE:
        status, message = _BY_REASON[reason]
        return TaskApiError(status, message, reason)
    if code in _BY_CODE:
        status, message, machine = _BY_CODE[code]
        return TaskApiError(status, message, machine)
    # Any other database error means the transaction was rolled back.
    return TaskApiError(503, UNAVAILABLE, "unavailable")


async def call_rpc(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /rest/v1/rpc/<name>. Send ALL parameters (the functions have no defaults)."""
    try:
        key = main._service_role_key()
        response = await main._request_supabase(
            f"/rest/v1/rpc/{name}",
            api_key=key,
            method="POST",
            json_body=payload,
            extra_headers={"Authorization": f"Bearer {key}"},
        )
    except (HTTPException, httpx.HTTPError) as error:
        cause = error.__cause__ if isinstance(error, HTTPException) else error
        if isinstance(cause, httpx.RequestError) and not isinstance(
            cause, main.CORRECTION_NOT_SENT_ERRORS
        ):
            logger.error("Task RPC %s outcome unconfirmed (%s).", name, type(cause).__name__)
            raise TaskApiError(504, UNCONFIRMED, "unconfirmed") from error
        logger.error("Task RPC %s was not sent (%s).", name, type(cause or error).__name__)
        raise TaskApiError(503, UNAVAILABLE, "unavailable") from error

    if not response.is_success:
        raise map_rpc_error(response)
    try:
        result = response.json()
    except ValueError as error:
        logger.error("Task RPC %s returned invalid JSON.", name)
        raise TaskApiError(502, UNAVAILABLE, "unavailable") from error
    if not isinstance(result, dict):
        logger.error("Task RPC %s returned an unexpected shape.", name)
        raise TaskApiError(502, UNAVAILABLE, "unavailable")
    return result
