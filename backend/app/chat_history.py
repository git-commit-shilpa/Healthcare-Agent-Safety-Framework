"""Persistent Patient chat history helpers (pure logic; no network, no database).

The Supabase RPC calls themselves live in main.py so they reuse `_request_supabase`.
Persistence is OFF unless HASF_CHAT_HISTORY_ENABLED is exactly "true".

Safety rules implemented here:
- Only turns that are clearly low-risk are stored. On any uncertainty the turn is NOT saved.
- Correction-flow turns are stored as fixed event text only. Their real text, any
  state token and any clinical details are never stored or restored.
- No redaction: a filtered turn is skipped, because the database compares the exact
  user text for turn-id idempotency.
"""

import asyncio
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import UUID

ENABLE_ENV = "HASF_CHAT_HISTORY_ENABLED"

# Fixed text stored for every correction-flow turn.
EVENT_USER_TEXT = "Correction request step"
EVENT_REPLY_TEXT = "Correction request step recorded. No details were saved."
EVENT_MODE = "event"
# Shown instead of a stored event reply: the step cannot be resumed from history.
REPLAY_EVENT_NOTICE = (
    "This step was already handled. If you still need a correction request, please start it again."
)

# Shown once, in place of any run of stored correction-flow placeholders when a saved
# conversation is reopened. It states plainly that the steps were not kept, and it keeps the
# correction request itself (its status and Physician review) separate from this history.
REDACTED_NOTICE = (
    "Correction-request steps in this conversation are not shown. For your privacy, what you "
    "typed and the details of those steps were never saved. Your correction requests and their "
    "review status are in AI Request History."
)

# Response modes that may be stored verbatim (everything else is refused or an event).
SAVEABLE_MODES = frozenset({"answer", "pointer", "greeting"})
# Intents the route itself assigns; "request_correction" marks correction-flow turns.
CORRECTION_INTENT = "request_correction"
GREETING_INTENT = "greeting"

SAVE_STATUSES = ("saved", "duplicate", "not_saved", "unconfirmed", "disabled")

# Why a turn was deliberately not saved (save_status "not_saved").
SKIP_FILTERED = "filtered"  # the privacy filter found something that must not be kept
SKIP_NOT_ELIGIBLE = "not_eligible"  # notices and other outcomes that are never saved
SKIP_SENSITIVE = "sensitive"  # the reply shows the patient's own record: never stored
SKIP_REFUSED = "refused"  # a refused or blocked exchange: never stored


def enabled() -> bool:
    """Read per call so the flag can be toggled without importing state. Default: off."""
    return os.getenv(ENABLE_ENV, "").strip() == "true"


class ChatHistoryRpcError(Exception):
    """A history RPC failed. `unconfirmed` means a write may have committed."""

    def __init__(self, db_code: str = "", *, unconfirmed: bool = False, status: int | None = None):
        super().__init__(db_code or "chat_history_rpc_error")
        self.db_code = db_code
        self.unconfirmed = unconfirmed
        self.status = status


# --- Classifying failed RPC responses --------------------------------------------------

# A SQLSTATE the database itself raised: the statement ran and its transaction was aborted,
# so nothing was written. Includes our PTnnn codes. PostgREST's own "PGRSTnnn" codes and
# gateway bodies are deliberately NOT recognized.
_POSTGRES_ERROR_CODE = re.compile(r"[0-9][0-9A-Z]{4}|P0[0-9]{3}|XX[0-9]{3}|HV[0-9A-Z]{3}|PT[0-9]{3}")


def is_postgres_error_code(code: str | None) -> bool:
    return bool(code) and _POSTGRES_ERROR_CODE.fullmatch(code) is not None


def http_failure_unconfirmed(status: int, db_code: str | None) -> bool:
    """True when an HTTP failure does not establish that nothing was written.

    The request reached the server. A 5xx without a recognized database error code could
    have been produced after the function committed (gateway, proxy, dropped response), so
    the outcome is unknown. 4xx responses are rejections, and a recognized SQLSTATE means
    the transaction was aborted.
    """
    return status >= 500 and not is_postgres_error_code(db_code)


# --- Privacy filter -------------------------------------------------------------------

_DB_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_-]{24,}[.][A-Za-z0-9_-]{24,}")  # same shape as the DB CHECK
_BEARER = re.compile(r"\bbearer\s+\S", re.IGNORECASE)
_LONG_OPAQUE = re.compile(r"[A-Za-z0-9_+/=-]{32,}")
_JWT_PREFIX = re.compile(r"eyJ[A-Za-z0-9_-]{8,}")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
_DIGIT_RUN = re.compile(r"\d{4,}")
_DATE = re.compile(
    r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_IDENTIFIERS = re.compile(
    r"\b(?:ssn|social security|date of birth|dob|mrn|medical record number|patient id|"
    r"insurance|policy number|passport|driver'?s? licen[cs]e|password|passcode|my name is|"
    r"my address|home address|credit card)\b",
    re.IGNORECASE,
)
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i’m|i've|i’ve|i'd|my|me|mine|myself)\b", re.IGNORECASE)
_HEALTH = re.compile(
    r"\b(?:diagnos\w*|medicat\w*|prescri\w*|treat\w*|dose|dosage|allerg\w*|symptom\w*|pain|"
    r"blood|pressure|surgery|surgical|cancer|diabet\w*|pregnan\w*|infection|illness|disease|"
    r"condition|therapy|tablet|pill|insulin|chemo\w*|mg|ml|mmhg|lab|results?|scan|"
    r"physician|doctor|record|correction|incorrect|wrong|mistake)\b",
    re.IGNORECASE,
)
_UNIT_NUMBER = re.compile(r"\d\s*(?:mg|mcg|ml|mmhg|kg|lbs?|units?|%)\b", re.IGNORECASE)


def _looks_unsafe(text: str, *, state_token: str | None, user_text: bool) -> bool:
    if state_token and state_token in text:
        return True
    if (
        _DB_TOKEN_LIKE.search(text)
        or _BEARER.search(text)
        or _LONG_OPAQUE.search(text)
        or _JWT_PREFIX.search(text)
    ):
        return True
    if (
        _EMAIL.search(text)
        or _PHONE.search(text)
        or _DIGIT_RUN.search(text)
        or _DATE.search(text)
        or _IDENTIFIERS.search(text)
        or _UNIT_NUMBER.search(text)
    ):
        return True
    # Anything that mixes first-person wording with health vocabulary may be personal
    # medical detail. Be conservative: skip it.
    if user_text and _FIRST_PERSON.search(text) and _HEALTH.search(text):
        return True
    return False


@dataclass(frozen=True)
class PersistPlan:
    user_text: str
    reply_text: str
    reply_mode: str


def evaluate_persistence(
    *,
    message: str,
    reply: str,
    mode: str,
    intent: str,
    state_token: str | None,
) -> tuple[PersistPlan | None, str | None]:
    """(plan, None) to store this turn, or (None, reason) to skip it.

    reason is SKIP_NOT_ELIGIBLE for outcomes that are never saved (refusals, notices,
    blocked or flagged output) and SKIP_FILTERED only when the privacy filter rejected
    otherwise-saveable content. Passing the NeMo rails does not make content safe to keep;
    this is an independent, conservative check on both sides of the turn.
    """
    if intent == CORRECTION_INTENT or state_token is not None:
        # Fixed event text only. The real text and any token are discarded here.
        return PersistPlan(EVENT_USER_TEXT, EVENT_REPLY_TEXT, EVENT_MODE), None
    if intent == GREETING_INTENT:
        # Canned greeting: the reply is fixed, but the user text must still be safe.
        pass
    elif mode == "record":
        return None, SKIP_SENSITIVE  # record details are shown in the chat but never kept
    elif mode == "refusal":
        return None, SKIP_REFUSED
    elif mode not in SAVEABLE_MODES:
        return None, SKIP_NOT_ELIGIBLE  # notices and other outcomes
    user_text = message.strip()
    reply_text = reply.strip()
    if not user_text or not reply_text or len(user_text) > 500 or len(reply_text) > 2000:
        return None, SKIP_NOT_ELIGIBLE
    if _looks_unsafe(user_text, state_token=state_token, user_text=True):
        return None, SKIP_FILTERED
    if _looks_unsafe(reply_text, state_token=state_token, user_text=False):
        return None, SKIP_FILTERED
    return PersistPlan(user_text, reply_text, mode), None


def present_messages(rows: list[Any]) -> list[Any]:
    """Turn stored rows into what a reopened conversation should show. Read-only: nothing is written.

    A correction-flow turn is stored as a fixed pair (a placeholder user row, then an assistant row
    with display_mode "event") because the real wording is deliberately never kept. Shown as-is it
    would look like the Patient said "Correction request step" over and over. Each run of such pairs
    is replaced by a single privacy notice; every other row is returned unchanged and in order.

    A user row is dropped only when it is exactly the fixed placeholder AND the row right after it is
    an assistant event row, so a Patient's own message is never hidden. Input rows are not modified.
    """
    out: list[Any] = []
    after_notice = False
    index = 0
    while index < len(rows):
        row = rows[index]
        following = rows[index + 1] if index + 1 < len(rows) else None
        is_pair = (
            isinstance(row, dict)
            and isinstance(following, dict)
            and row.get("role") == "user"
            and row.get("content") == EVENT_USER_TEXT
            and following.get("role") == "assistant"
            and following.get("display_mode") == EVENT_MODE
        )
        is_event = isinstance(row, dict) and row.get("role") == "assistant" and row.get("display_mode") == EVENT_MODE
        if is_pair or is_event:
            if not after_notice:
                out.append({
                    "role": "assistant",
                    "content": REDACTED_NOTICE,
                    "display_mode": EVENT_MODE,
                    "created_at": row.get("created_at"),
                })
                after_notice = True
            index += 2 if is_pair else 1
            continue
        out.append(dict(row) if isinstance(row, dict) else row)
        after_notice = False
        index += 1
    return out


def persistence_plan(**kwargs: Any) -> PersistPlan | None:
    """Plan only; see evaluate_persistence for the reason a turn is skipped."""
    return evaluate_persistence(**kwargs)[0]


def find_candidates(message: str, state_token: str | None) -> list[str]:
    """User texts to try with find_patient_chat_turn, in order.

    A stored correction-flow turn holds the fixed label, not the real message, so a
    replay must be matched against the label too.
    """
    if state_token is not None:
        return [EVENT_USER_TEXT]
    text = message.strip()
    if _looks_unsafe(text, state_token=None, user_text=True):
        # Such a message could never have been stored as-is, and it is not sent to the
        # database at all (it may hold personal or clinical detail).
        return [EVENT_USER_TEXT]
    return [text, EVENT_USER_TEXT]


# --- Per-process in-flight coordination -----------------------------------------------

_inflight: dict[tuple[int, UUID], list[Any]] = {}


@asynccontextmanager
async def inflight(user_id: int, turn_id: UUID) -> AsyncIterator[None]:
    """Serialize identical (user_id, turn_id) requests inside this process.

    The second request waits, then finds the first one's saved turn and replays it
    instead of calling the model again. This does not coordinate across worker
    processes; the database (advisory lock + unique turn_id) remains the
    authority there, so a cross-worker duplicate may still call the model twice
    but is saved only once.
    """
    key = (user_id, turn_id)
    entry = _inflight.get(key)
    if entry is None:
        entry = [asyncio.Lock(), 0]
        _inflight[key] = entry
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _inflight.get(key) is entry:
            del _inflight[key]
