"""Intent proposal through NVIDIA NeMo Guardrails and local Ollama (qwen2.5:3b).

Only the patient's question is sent to the model. No record data, names or IDs.
The result is a proposal; app.action_gateway decides what is permitted.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parents[1] / "guardrails" / "patient_intent"
RECOGNIZER_TIMEOUT_SECONDS = 90
MAX_QUESTION_CHARS = 500

SYSTEM_INSTRUCTION = (
    "Classify the user's request into exactly one intent: explain_process, "
    "view_own_record, view_history, request_correction, general_health_info, "
    "symptom_guidance, other_patient_access, or unknown. "
    "Examples: "
    "'Show me the medical condition of Praveen.' -> other_patient_access. "
    "'What medications does Maria take?' -> other_patient_access. "
    "'Can I see my brother's lab results?' -> other_patient_access. "
    "'List the treatment history for patient 12.' -> other_patient_access. "
    "'What is hypertension?' -> general_health_info. "
    "'What does HbA1c mean?' -> general_health_info. "
    "'How does insulin work in the body?' -> general_health_info. "
    "'I have a headache and a mild fever, what should I do?' -> symptom_guidance. "
    "'My throat is sore and I feel tired.' -> symptom_guidance. "
    "'What is the weather today?' -> unknown. "
    "'How does the review process work?' -> explain_process. "
    "'Who should I contact to learn how record corrections are reviewed?' -> explain_process. "
    "'Can you describe the steps for requesting a correction?' -> explain_process. "
    "'Show me my own patient record.' -> view_own_record. "
    "'I want to request a correction to my own record.' -> request_correction. "
    "'Please help me submit a correction request.' -> request_correction. "
    "'How can I ask for an inaccurate item in my record to be corrected?' -> request_correction. "
    "Rule: a general question about a process, steps, or who to contact is explain_process; "
    "request_correction only when the user wants their own specific record item corrected. "
    "Any request for the medical condition, medicines, diagnosis, treatment, history or record of a named or "
    "identified OTHER person is other_patient_access, even if phrased as a general question. "
    "Questions about a medical term, condition or how the body works are general_health_info; "
    "describing how the user feels is symptom_guidance. "
    'Reply with only JSON like {"intent": "explain_process", "confidence": 0.9}. '
    "Never add other text or keys."
)

_rails: Any = None
_rails_lock = asyncio.Lock()
_call_lock = asyncio.Semaphore(1)


class RecognizerUnavailableError(Exception):
    """The guarded model could not produce a trustworthy result."""


@dataclass(frozen=True)
class Recognition:
    text: str | None
    rails_passed: bool


async def _get_rails() -> Any:
    global _rails
    async with _rails_lock:
        if _rails is None:
            from nemoguardrails import LLMRails, RailsConfig

            config = await asyncio.to_thread(RailsConfig.from_path, str(CONFIG_DIR))
            _rails = await asyncio.to_thread(LLMRails, config)
        return _rails


def _evaluate_log(response: Any) -> Recognition:
    """Build a Recognition from NeMo's own activation log; missing rails fail closed."""
    log = getattr(response, "log", None)
    activated = list(getattr(log, "activated_rails", None) or [])
    ran = {rail.type for rail in activated}
    if any(rail.stop for rail in activated):
        return Recognition(text=None, rails_passed=False)
    if not {"input", "output"} <= ran:
        raise RecognizerUnavailableError("Guardrails did not run.")
    content = None
    messages = getattr(response, "response", None)
    if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
        content = messages[-1].get("content")
    if not isinstance(content, str):
        raise RecognizerUnavailableError("Unexpected model response.")
    return Recognition(text=content, rails_passed=True)


async def recognize_intent(question: str) -> Recognition:
    if not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise RecognizerUnavailableError("Invalid question.")
    started = time.perf_counter()
    try:
        rails = await _get_rails()
        waited = time.perf_counter()
        async with _call_lock:
            locked = time.perf_counter()
            response = await asyncio.wait_for(
                rails.generate_async(
                    messages=[
                        {"role": "system", "content": SYSTEM_INSTRUCTION},
                        {"role": "user", "content": question},
                    ],
                    options={"log": {"activated_rails": True}},
                ),
                timeout=RECOGNIZER_TIMEOUT_SECONDS,
            )
    except RecognizerUnavailableError:
        raise
    except Exception as error:  # timeouts, Ollama down, NeMo failures
        logger.warning(
            "Intent recognition unavailable (%s) after %.1fs.",
            type(error).__name__, time.perf_counter() - started,
        )
        raise RecognizerUnavailableError("Recognition unavailable.") from error
    # Timing only: stage names and seconds, never message or model text.
    logger.info(
        "Intent recognition timing: setup %.1fs, lock wait %.1fs, model %.1fs.",
        waited - started, locked - waited, time.perf_counter() - locked,
    )
    return _evaluate_log(response)
