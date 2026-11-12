"""Patient dependencies for service requests."""

from typing import Annotated

from fastapi import Depends, HTTPException

from app.main import CurrentUser, require_patient
from app.technician.features import require_feature_enabled

# Added by migration 011 (not yet applied to the live project).
SUBMIT_SERVICE_REQUEST = "submit_service_request"
VIEW_OWN_SERVICE_REQUESTS = "view_own_service_requests"


def require_patient_permission(action: str):
    """Order: authenticated -> Patient role -> feature enabled -> permission row."""

    async def check(user: Annotated[CurrentUser, Depends(require_patient)]) -> CurrentUser:
        require_feature_enabled()
        if action not in user.permissions:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action.",
            )
        return user

    return check
