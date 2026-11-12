"""Feature gate for patient service requests and Technician tasks.

OFF unless HASF_SERVICE_REQUESTS_ENABLED is exactly "true" (same convention as
HASF_CHAT_HISTORY_ENABLED). Keep it off until migrations 010-012 have been applied
to the live Supabase project and verified. Read at call time so tests and a restart
with a new .env behave predictably.
"""

import os

from app.technician.errors import TaskApiError

FEATURE_DISABLED_DETAIL = "This feature is not available yet."


def service_requests_enabled() -> bool:
    return os.getenv("HASF_SERVICE_REQUESTS_ENABLED", "").strip() == "true"


def require_feature_enabled() -> None:
    if not service_requests_enabled():
        raise TaskApiError(503, FEATURE_DISABLED_DETAIL, "feature_disabled")
