"""Technician role dependencies and permission names."""

from typing import Annotated

from fastapi import Depends, HTTPException

from app.main import CurrentUser, Role, get_current_user
from app.technician.features import require_feature_enabled

# Names verified against the live Supabase `permissions` table. The live
# Technician rows with allowed=false (update_medical_record,
# view_unassigned_patient) are deliberately not listed: they must stay denied.
VIEW_ASSIGNED_PATIENT = "view_assigned_patient"
REQUEST_AI_ASSISTANCE = "request_ai_assistance"
UPLOAD_TEST_RESULT = "upload_test_result"
# Added by migration 011 (not yet applied to the live project).
VIEW_ASSIGNED_TASK = "view_assigned_task"
UPDATE_TASK_STATUS = "update_task_status"

TECHNICIAN_ACTIONS = (
    VIEW_ASSIGNED_PATIENT,
    REQUEST_AI_ASSISTANCE,
    UPLOAD_TEST_RESULT,
    VIEW_ASSIGNED_TASK,
    UPDATE_TASK_STATUS,
)


async def require_technician(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if user.role != Role.TECHNICIAN:
        raise HTTPException(status_code=403, detail="Technician role is required.")
    return user


def require_technician_permission(action: str):
    """Technician-only dependency that also requires one permission row."""

    async def check_permission(
        user: Annotated[CurrentUser, Depends(require_technician)],
    ) -> CurrentUser:
        if action not in user.permissions:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action.",
            )
        return user

    return check_permission


def require_task_permission(action: str):
    """Order: authenticated -> Technician role -> feature enabled -> permission row.

    An unauthenticated or wrong-role caller never learns whether the feature is on.
    """

    async def check(
        user: Annotated[CurrentUser, Depends(require_technician)],
    ) -> CurrentUser:
        require_feature_enabled()
        if action not in user.permissions:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action.",
            )
        return user

    return check
