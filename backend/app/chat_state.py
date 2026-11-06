"""Server-validated, single-use conversation state for the Patient assistant.

A state token is HMAC-signed and bound to the authenticated user, their login
session, one action, one step and an expiry. The confirm step is also bound to
a keyed hash of the exact proposed text, so only that text can be confirmed.
Tokens carry no clinical text and are consumed on first use, so a replayed
token is rejected. Consumed nonces live in memory only (reset on restart; not
shared across workers) -- the database duplicate guard (migration 007) remains
the real backstop for submissions.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

STATE_TTL_SECONDS = 5 * 60
MAX_TOKEN_CHARS = 600
ACTION = "request_correction"
STEP_OFFERED = "offered"
STEP_DETAILS = "details"
STEP_CONFIRM = "confirm"
_STEPS = {STEP_OFFERED, STEP_DETAILS, STEP_CONFIRM}

# Per-process secret unless one is configured. Restarting invalidates open steps (fails closed).
_SECRET = (os.getenv("HASF_CHAT_STATE_SECRET") or "").encode() or secrets.token_bytes(32)
_consumed: dict[str, float] = {}

_YES = frozenset({"yes", "yes please", "yeah", "yep", "sure", "ok", "okay", "please do"})
_NO = frozenset({"no", "no thanks", "no thank you", "nope", "not now"})
_CANCEL = frozenset({"cancel", "stop", "never mind", "nevermind", "exit"})


class InvalidStateError(Exception):
    """Forged, tampered, expired, cross-user, cross-session, wrong-text or replayed state."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())


def bind_text(text: str) -> str:
    """Keyed hash that ties a confirm token to one exact proposal without storing it."""
    return hmac.new(_SECRET, b"text:" + text.encode(), hashlib.sha256).hexdigest()


def issue_state(
    user_id: int, step: str, now: float | None = None, *, session_key: str = "", proposal_hash: str = ""
) -> str:
    if step not in _STEPS:
        raise ValueError("unknown step")
    issued = time.time() if now is None else now
    payload = {
        "u": user_id, "k": session_key, "a": ACTION, "s": step, "h": proposal_hash,
        "e": int(issued + STATE_TTL_SECONDS), "n": secrets.token_hex(8),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def _validate(token: Any, user_id: int, now: float | None, session_key: str) -> dict[str, Any]:
    current = time.time() if now is None else now
    try:
        if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
            raise InvalidStateError
        body, _, signature = token.partition(".")
        if not body or not hmac.compare_digest(signature, _sign(body)):
            raise InvalidStateError
        payload = json.loads(_unb64(body))
        if (
            not isinstance(payload, dict)
            or isinstance(payload.get("u"), bool)
            or payload.get("u") != user_id
            or payload.get("k") != session_key
            or payload.get("a") != ACTION
            or payload.get("s") not in _STEPS
            or not isinstance(payload.get("h"), str)
            or not isinstance(payload.get("e"), int)
            or not isinstance(payload.get("n"), str)
        ):
            raise InvalidStateError
    except InvalidStateError:
        raise
    except Exception as error:  # malformed base64/json
        raise InvalidStateError from error
    if payload["e"] < current:
        raise InvalidStateError
    for nonce, expires in list(_consumed.items()):
        if expires < current:
            del _consumed[nonce]
    if payload["n"] in _consumed:
        raise InvalidStateError
    return payload


def peek_state(token: Any, user_id: int, now: float | None = None, *, session_key: str = "") -> str:
    """Validate without consuming. Returns the step."""
    return _validate(token, user_id, now, session_key)["s"]


def consume_state(
    token: Any, user_id: int, now: float | None = None, *,
    session_key: str = "", proposal_hash: str = "", ignore_hash: bool = False,
) -> str:
    """Validate for this user/session (and exact text, for confirm) and mark it used. Returns its step."""
    payload = _validate(token, user_id, now, session_key)
    if not ignore_hash and not hmac.compare_digest(payload["h"], proposal_hash):
        raise InvalidStateError
    _consumed[payload["n"]] = float(payload["e"])
    return payload["s"]


def interpret_reply(message: str) -> str | None:
    """Return 'yes', 'no', 'cancel' for an exact short reply, else None."""
    if len(message) > 40:
        return None
    text = " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in message.casefold()).split())
    if text in _YES:
        return "yes"
    if text in _NO:
        return "no"
    if text in _CANCEL:
        return "cancel"
    return None
