"""Feature gate for appointment scheduling.

OFF unless HASF_APPOINTMENTS_ENABLED is exactly "true" AND the service-request gate is on
(appointments are technician tasks). Keep both off until migrations 010-013 have been
applied to the live project and verified.
"""

import os

from app.technician.errors import TaskApiError
from app.technician.features import FEATURE_DISABLED_DETAIL, service_requests_enabled


def appointments_enabled() -> bool:
    return os.getenv("HASF_APPOINTMENTS_ENABLED", "").strip() == "true" and service_requests_enabled()


def require_appointments_enabled() -> None:
    if not appointments_enabled():
        raise TaskApiError(503, FEATURE_DISABLED_DETAIL, "feature_disabled")
