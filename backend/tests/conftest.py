"""Makes the backend unit tests independent of the developer's real backend/.env.

app.main loads backend/.env when it is imported, so feature flags that are switched on there (service requests,
appointments, chat history) would otherwise leak into tests that assume a particular state. Every test starts with all
of them OFF, exactly like a fresh checkout. A test that needs a flag ON (or an explicit OFF) sets it itself with
``patch.dict(os.environ, {...})``, which is how both states are covered.
"""

import pytest

FEATURE_FLAGS = (
    "HASF_SERVICE_REQUESTS_ENABLED",
    "HASF_APPOINTMENTS_ENABLED",
    "HASF_CHAT_HISTORY_ENABLED",
)


@pytest.fixture(autouse=True)
def _feature_flags_start_off(monkeypatch):
    for name in FEATURE_FLAGS:
        monkeypatch.setenv(name, "")
    yield
