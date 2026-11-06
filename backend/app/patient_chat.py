"""Conversational replies for the Patient assistant (Phase 1, no actions, no writes).

The patient's message is the only text sent to the model. No record data,
names, IDs or credentials are ever included. The Action Gateway still decides
which requests are actionable; this module only produces safe wording.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import output_safety
from app.action_gateway import HEALTH_INTENTS, GatewayDecision
from app.intent_recognizer import Recognition, RecognizerUnavailableError, _evaluate_log

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "guardrails" / "patient_chat"
HEALTH_CONFIG_DIR = Path(__file__).resolve().parents[1] / "guardrails" / "patient_health"
CHAT_TIMEOUT_SECONDS = 90
MAX_REPLY_CHARS = 1200

SYSTEM_PROMPT = (
    "You are a friendly helper inside the HASF patient portal. Answer in at most "
    "four short sentences, in plain language. You know these pages: Overview; "
    "My Medical Records (view your own record and request a treatment correction, "
    "which is reviewed by your assigned Physician and never changes the record "
    "immediately); AI Request History (the status of your correction requests only); Ask AI Assistant; "
    "My Profile. Only one correction request can be waiting for review at a time. "
    "Never give medical advice, diagnosis or treatment suggestions. Never claim to "
    "have changed, approved or submitted anything. You cannot see the user's record."
)

HEALTH_SYSTEM_PROMPT = (
    "You are an educational helper inside the HASF patient portal. You are an AI assistant, "
    "not a doctor, nurse or any healthcare professional, and you never say or imply otherwise. "
    "Answer in at most five short sentences, in plain language. Explain medical terms and general "
    "health topics using well-established, widely accepted information only. If you are not sure "
    "of a fact, say you are not sure and suggest asking a Physician; never invent facts, "
    "statistics, studies or drug doses. Never diagnose the user or tell them what condition they "
    "have, never prescribe or recommend a specific medicine or dose, and never tell them to stop "
    "or change a treatment. Never say 'you have', 'you might have' or 'it sounds like you have' "
    "about any condition; instead say symptoms can have many causes. If they describe symptoms, give only cautious general information "
    "(for example rest, fluids, and when to seek medical care) and encourage contacting their "
    "Physician. You cannot see the user's record. Never claim to have changed anything."
)
HEALTH_DISCLAIMER = (
    "\n\nThis is general information, not medical advice or a diagnosis. "
    "Please talk to your Physician about your own health."
)
SYMPTOM_NOTICE = (
    " If your symptoms are severe, sudden or getting worse, seek urgent medical care or call "
    "your local emergency number."
)

REFUSALS = {
    "guardrail_blocked": "I'm sorry, I can't help with that. I can explain medical terms and general health topics, how the portal works, how correction requests are reviewed, or where to find your record and request history. Could you rephrase your question?",
    "permission_missing": "Your account doesn't currently have access to that. I can still explain how the portal works.",
    "invalid_model_output": "I didn't quite understand that. Could you rephrase your question?",
    "role_not_permitted": "This assistant is available to patient accounts only.",
}
ESCALATION_REPLY = (
    "I can't change or submit corrections from this chat. Correction requests are reviewed by "
    "your assigned Physician. You can start one in My Medical Records."
)
POINTERS = {
    "view_own_record": "You can see your own record in My Medical Records. For your privacy I don't read it out in this chat.",
    "view_history": "The status of your correction requests is in AI Request History.",
}
OUTPUT_BLOCKED_REPLY = "I'm sorry, I can't share that. Is there something about the portal I can explain?"


_GREETINGS = frozenset({
    "hello", "hi", "hey", "hello there", "hi there", "hey there",
    "good morning", "good afternoon", "good evening", "thanks", "thank you",
})
_CAPABILITY = frozenset({
    "what can you do", "what can you help me with", "what can you help with",
    "how can you help", "how can you help me", "what do you do", "who are you",
})
GREETING_REPLY = (
    "Hello! I can explain medical terms and general health topics, how the portal works, how "
    "correction requests are reviewed, and where to find your record and request history. I can't "
    "diagnose, prescribe, or change your record."
)


def _normalise(message: str) -> str:
    return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in message.casefold()).split())


def canned_reply(message: str) -> "ChatReply | None":
    """Fixed reply for an exact simple greeting or capability question, else None.

    Matching is whole-message only, so anything with extra words (including
    instructions appended to a greeting) falls through to the guarded path.
    """
    if len(message) > 60:
        return None
    text = _normalise(message)
    if text in _GREETINGS or text in _CAPABILITY:
        return ChatReply(GREETING_REPLY, "greeting")
    for greeting in _GREETINGS:
        if text.startswith(greeting + " ") and text[len(greeting) + 1:] in _CAPABILITY:
            return ChatReply(GREETING_REPLY, "greeting")
    return None


class _OutputGate:
    """Compatibility wrapper: .search(text) is truthy when the deterministic check stops the text."""

    @staticmethod
    def search(text: str):
        result = output_safety.check(text)
        return None if result.passed else result.category


_UNSAFE_HEALTH_OUTPUT = _OutputGate()
# Where a health reply was stopped (safe codes only, never text). Mapped to reason codes in main.py.
BLOCKED_BY_INPUT_RAIL = "input_rail"
BLOCKED_BY_OUTPUT_RAIL = "output_rail"
BLOCKED_BY_OUTPUT_CHECK = "output_check"
HEALTH_RETRY_LIMIT = 1
# A retry doubles the wait, so it is only worth trying when the first attempt was quick.
HEALTH_RETRY_MAX_FIRST_SECONDS = 25.0


# --- Replies built only from the patient's own retrieved record (no model involved) ---------

RECORD_MISSING_REPLY = (
    "No medical record is linked to your account yet, so there is nothing to show. "
    "Please contact your care team if you expected one."
)
RECORD_UNAVAILABLE_REPLY = (
    "I couldn't retrieve your record right now, so I can't tell you what it says. "
    "Please try again in a moment or open My Medical Records."
)
RECORD_FOOTER = (
    "This is what is recorded in your portal. It is not a new diagnosis or medical advice. "
    "Your assigned Physician can explain it, and you can request a correction in My Medical Records."
)
MAX_RECORD_FIELD_CHARS = 400


def _clip(value: str) -> str:
    value = " ".join(value.split())
    return value if len(value) <= MAX_RECORD_FIELD_CHARS else value[: MAX_RECORD_FIELD_CHARS - 1] + "…"


def requested_record_fields(message: str) -> tuple[str, ...]:
    """Only the fields the question asks about (data minimisation); a general ask returns all three."""
    text = _normalise(message)
    fields = tuple(
        name
        for name, words in (
            ("medical_condition", ("condition", "conditions")),
            ("treatment", ("treatment", "treatments", "treated")),
            ("age", ("age", "old")),
        )
        if any(word in text.split() for word in words)
    )
    return fields or ("medical_condition", "treatment", "age")


def record_reply(
    message: str, *, age: int | None, medical_condition: str | None, treatment: str | None
) -> str:
    """Grounded wording from the retrieved fields only; a missing value is stated as not recorded."""
    values = {"medical_condition": medical_condition, "treatment": treatment, "age": age}
    labels = {"medical_condition": "Condition", "treatment": "Treatment", "age": "Age"}
    lines = []
    for name in requested_record_fields(message):
        value = values[name]
        shown = "not recorded" if value is None or (isinstance(value, str) and not value.strip()) else _clip(str(value))
        lines.append(f"{labels[name]}: {shown}")
    return "Here is what your record shows.\n" + "\n".join(lines) + "\n\n" + RECORD_FOOTER


class ChatUnavailableError(Exception):
    """The guarded chat model could not produce a trustworthy reply."""


@dataclass(frozen=True)
class ChatReply:
    reply: str
    mode: str  # answer | refusal | notice | pointer
    # Safe diagnostic only: which component stopped a refused reply (BLOCKED_BY_*), plus the
    # deterministic category when the output check did it. Never any message or model text.
    blocked_by: str | None = None
    blocked_detail: str | None = None


_rails: Any = None
_health_rails: Any = None
_rails_lock = asyncio.Lock()
_call_lock = asyncio.Semaphore(1)


async def _get_rails() -> Any:
    global _rails
    async with _rails_lock:
        if _rails is None:
            from nemoguardrails import LLMRails, RailsConfig

            config = await asyncio.to_thread(RailsConfig.from_path, str(CONFIG_DIR))
            _rails = await asyncio.to_thread(LLMRails, config)
        return _rails


async def _get_health_rails() -> Any:
    global _health_rails
    async with _rails_lock:
        if _health_rails is None:
            from nemoguardrails import LLMRails, RailsConfig

            config = await asyncio.to_thread(RailsConfig.from_path, str(HEALTH_CONFIG_DIR))
            _health_rails = await asyncio.to_thread(LLMRails, config)
        return _health_rails


def _evaluate_health_log(response: Any) -> Recognition:
    """Health route: the NeMo input rail must have run; the output gate is output_safety (in code)."""
    log = getattr(response, "log", None)
    activated = list(getattr(log, "activated_rails", None) or [])
    if any(rail.stop for rail in activated):
        return Recognition(text=None, rails_passed=False)
    if "input" not in {rail.type for rail in activated}:
        raise RecognizerUnavailableError("Guardrails did not run.")
    messages = getattr(response, "response", None)
    content = messages[-1].get("content") if isinstance(messages, list) and messages and isinstance(messages[-1], dict) else None
    if not isinstance(content, str):
        raise RecognizerUnavailableError("Unexpected model response.")
    return Recognition(text=content, rails_passed=True)


def _stopped_by(response: Any) -> str:
    """Which NeMo rail stopped the exchange, as a safe code (rail names only, no text)."""
    activated = list(getattr(getattr(response, "log", None), "activated_rails", None) or [])
    for rail in activated:
        if rail.stop:
            return BLOCKED_BY_INPUT_RAIL if rail.type == "input" else BLOCKED_BY_OUTPUT_RAIL
    return BLOCKED_BY_OUTPUT_RAIL


async def generate_reply(
    message: str, system_prompt: str = SYSTEM_PROMPT, profile: str = "chat"
) -> ChatReply:
    """One guarded generation. Missing rails, errors or timeouts raise ChatUnavailableError.

    profile="health" is the general-health-education route: NeMo input rail plus the
    deterministic output check in app/output_safety.py instead of the LLM output judge.
    """
    health = profile == "health"
    started = time.perf_counter()
    try:
        rails = await (_get_health_rails() if health else _get_rails())
        waited = time.perf_counter()
        async with _call_lock:
            locked = time.perf_counter()
            response = await asyncio.wait_for(
                rails.generate_async(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message},
                    ],
                    options={"log": {"activated_rails": True}},
                ),
                timeout=CHAT_TIMEOUT_SECONDS,
            )
        recognition = _evaluate_health_log(response) if health else _evaluate_log(response)
    except RecognizerUnavailableError as error:
        raise ChatUnavailableError("Guardrails did not run.") from error
    except Exception as error:  # timeouts, Ollama down, NeMo failures
        logger.warning(
            "Chat generation unavailable (%s) after %.1fs.",
            type(error).__name__, time.perf_counter() - started,
        )
        raise ChatUnavailableError("Chat unavailable.") from error
    logger.info(
        "Chat generation timing: setup %.1fs, lock wait %.1fs, model %.1fs.",
        waited - started, locked - waited, time.perf_counter() - locked,
    )
    if not recognition.rails_passed:
        # A rail stopped the exchange; show a fixed safe message, never model text.
        source = _stopped_by(response)
        logger.info("Chat reply blocked: source=nemo_%s profile=%s.", source, profile)
        return ChatReply(OUTPUT_BLOCKED_REPLY, "refusal", blocked_by=source)
    text = (recognition.text or "").strip()
    if not text or len(text) > MAX_REPLY_CHARS:
        raise ChatUnavailableError("Invalid reply.")
    if health:
        verdict = output_safety.check(text)
        if not verdict.passed:
            logger.info("Chat reply blocked: source=output_check category=%s profile=health.", verdict.category)
            return ChatReply(
                OUTPUT_BLOCKED_REPLY, "refusal",
                blocked_by=BLOCKED_BY_OUTPUT_CHECK, blocked_detail=verdict.category,
            )
    return ChatReply(text, "answer")


async def _health_answer(answer, message: str) -> ChatReply:
    """Educational answer (NeMo input rail + deterministic output check). One quick retry if stopped."""
    reply = ChatReply(OUTPUT_BLOCKED_REPLY, "refusal")
    for _ in range(HEALTH_RETRY_LIMIT + 1):
        started = time.perf_counter()
        reply = await answer(message, system_prompt=HEALTH_SYSTEM_PROMPT, profile="health")
        if reply.mode == "answer":
            verdict = output_safety.check(reply.reply)  # also covers a stubbed or replaced generator
            if verdict.passed:
                return reply
            reply = ChatReply(
                OUTPUT_BLOCKED_REPLY, "refusal",
                blocked_by=BLOCKED_BY_OUTPUT_CHECK, blocked_detail=verdict.category,
            )
        if time.perf_counter() - started > HEALTH_RETRY_MAX_FIRST_SECONDS:
            break  # too slow to afford a second attempt inside the turn's time budget
    return reply


async def converse(message: str, gateway: GatewayDecision, answer=None) -> ChatReply:
    """Turn the gateway's verdict into wording. Only informational replies reach the model."""
    answer = answer or generate_reply
    if gateway.decision == "Escalate":
        return ChatReply(ESCALATION_REPLY, "notice")
    if gateway.decision == "Allow":
        if gateway.intent in POINTERS:
            return ChatReply(POINTERS[gateway.intent], "pointer")
        if gateway.intent in HEALTH_INTENTS:
            reply = await _health_answer(answer, message)
            if reply.mode != "answer":  # still stopped after the retry: keep the fixed refusal
                return reply
            # The disclaimer is added in code so the model can never leave it out.
            notice = SYMPTOM_NOTICE if gateway.intent == "symptom_guidance" else ""
            return ChatReply(reply.reply.rstrip() + notice + HEALTH_DISCLAIMER, "answer")
        return await answer(message)
    # Block: an unrecognised but guardrail-approved message is treated as a general
    # question and answered under the same input/output rails. It never triggers an action.
    if gateway.reason_code == "unknown_intent":
        return await answer(message)
    return ChatReply(REFUSALS.get(gateway.reason_code, REFUSALS["guardrail_blocked"]), "refusal")
