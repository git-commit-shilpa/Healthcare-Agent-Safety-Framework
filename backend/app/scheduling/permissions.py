"""Patient dependencies for appointments."""

from typing import Annotated

from fastapi import Depends, HTTPException

from app.main import CurrentUser, require_patient
from app.scheduling.features import require_appointments_enabled

# Added by migration 013 (not yet applied to the live project).
REQUEST_APPOINTMENT = "request_appointment"
VIEW_PHYSICIAN_AVAILABILITY = "view_physician_availability"
# Added by migration 011.
VIEW_OWN_SERVICE_REQUESTS = "view_own_service_requests"


def require_patient_appointment_permission(*actions: str):
    """Order: authenticated -> Patient role -> feature enabled -> every listed permission row."""

    async def check(user: Annotated[CurrentUser, Depends(require_patient)]) -> CurrentUser:
        require_appointments_enabled()
        if any(action not in user.permissions for action in actions):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action.",
            )
        return user

    return check
