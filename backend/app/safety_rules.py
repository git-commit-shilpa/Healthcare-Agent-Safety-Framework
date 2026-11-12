"""Deterministic pre-screen for the Patient assistant (runs before any language model).

The small local model is not trusted to spot emergencies or to refuse requests that
must never be served. This module matches fixed patterns on the patient's message
and returns a fixed, reviewed reply. Anything it does not recognise returns None and
continues to the guarded model path and the Action Gateway.

False positives are deliberately cheap (a referral to a physician or to emergency
help); false negatives are what the Action Gateway and NeMo rails still back up.
"""

import re
from dataclasses import dataclass
from typing import Literal

from app import third_party_access

Mode = Literal["notice", "refusal"]


@dataclass(frozen=True)
class SafetyVerdict:
    intent: str
    decision: Literal["Block", "Escalate"]
    reason_code: str
    reply: str
    mode: Mode


EMERGENCY_REPLY = (
    "This could be a medical emergency. Please call your local emergency number now "
    "(for example 911 in the US) or go to the nearest emergency department. If you are "
    "thinking about harming yourself, contact emergency services or a crisis line right "
    "away (for example 988 in the US). I can't assess emergencies from this chat."
)
OTHER_PATIENT_REPLY = (
    "I can only help with your own information. I can't access or share another "
    "person's records or details."
)
PRESCRIPTION_REPLY = (
    "I can't prescribe, refill or recommend medication. Please contact your Physician, "
    "who can review your situation and prescribe if appropriate."
)
DOSAGE_REPLY = (
    "I can't tell you how much of a medicine to take or whether to change it, because the right "
    "amount depends on you. Please ask your Physician or pharmacist, and follow the instructions "
    "on your medicine."
)
DIAGNOSIS_REPLY = (
    "I can't diagnose conditions or interpret your personal health situation. Your "
    "recorded information is in My Medical Records, and your Physician can explain it. "
    "I'm happy to explain a medical term in general."
)
APPROVAL_BYPASS_REPLY = (
    "I can't approve anything or skip your Physician's review. Changes to your record are only made "
    "after your assigned Physician reviews a correction request. I can explain how that works."
)
# Asking the assistant to approve, or to get something done without the Physician's review.
_APPROVAL_BYPASS = (
    re.compile(r"\b(?:bypass|circumvent|skip|avoid|get around)\b.{0,25}\b(?:physician|doctor|review|approval|sign[- ]?off)\b"),
    re.compile(
        r"\b(?:approve|apply|update|change|submit|finali[sz]e|accept|authori[sz]e|confirm|edit|modify|make)\b"
        r".{0,60}\bwithout\b.{0,30}\b(?:physician|doctor|review|approval)\b"
    ),
    re.compile(r"\bno need (?:for|to) (?:a |the |my )?(?:physician|doctor|review|approval)\b"),
    re.compile(r"\bauto[- ]?approve\b|\bapprove (?:it|this|that) (?:myself|automatically)\b"),
    re.compile(r"\bapprove my (?:own )?(?:correction|request|change|treatment|record)\b"),
)
RECORD_CHANGE_REPLY = (
    "I can't change your record. If something in it looks wrong, you can request a "
    "correction in My Medical Records, and your assigned Physician will review it."
)

_PRONOUN = r"(?:i|i'm|im|i've|ive|my|me|we|he|she|they|him|her|them|someone|somebody|mom|dad|mother|father|husband|wife|child|baby|son|daughter|friend|patient)"
_PERSON_REF = re.compile(rf"\b{_PRONOUN}\b")

# Always escalate, even without a pronoun.
_EMERGENCY_ALWAYS = re.compile(
    r"\b(?:suicid\w*|kill (?:myself|himself|herself|themselves)|end (?:my|his|her) life|"
    r"want to die|self[- ]?harm|hurt myself|harm myself|overdos\w*|anaphyla\w*|"
    r"call (?:911|999|112|an ambulance)|ambulance|medical emergency)\b"
)
# Escalate when a person is plausibly describing a current situation.
_EMERGENCY_SYMPTOM = re.compile(
    r"\b(?:chest (?:pain|pressure|tightness)|pain in (?:my|the) chest|heart attack|stroke|"
    r"can'?t breathe|cannot breathe|can not breathe|(?:trouble|difficulty|struggling) breathing|"
    r"not breathing|stopped breathing|gasping|choking|unconscious|unresponsive|passed out|fainted|"
    r"seizure|convuls\w*|severe bleeding|bleeding (?:heavily|won'?t stop|a lot)|coughing (?:up )?blood|"
    r"vomiting blood|face (?:is )?droop\w*|slurred speech|numb(?:ness)? on one side|"
    r"swollen (?:throat|tongue)|throat (?:is )?closing|poison\w*|severe allergic)\b"
)

_OTHER_PERSON = re.compile(
    r"\b(?:another|other|different) (?:patients?|people|persons?|users?)\b"
    r"|\b(?:someone|somebody|anyone|everyone) else'?s?\b"
    r"|\b(?:all|every|each) (?:the )?patients?\b"
    r"|\bpatients?\s*(?:id\s*)?#?\d+\b"
    r"|\bpatients? named\b"
    r"|\bmy (?:wife|husband|mother|father|mom|dad|friend|neighbou?r|son|daughter|brother|sister|"
    r"colleague|coworker|boss|ex|partner|girlfriend|boyfriend)'?s?\b"
    # A possessive name ("john smith's diagnosis"); contractions such as "what's" are excluded.
    r"|\b(?!(?:what|that|there|here|it|let|who|how|where|when|why|which|everything|nothing|something|anything)'s)"
    r"[a-z]{2,}'s (?:medical |health )?(?:records?|diagnos\w+|conditions?|treatments?|medications?|"
    r"history|results?|charts?|files?|information|info|data|details|age|address|phone|email|labs?)\b"
)
# Case-sensitive: a capitalised name right after a title ("patient John", "Mr. Smith").
_NAMED_PERSON = re.compile(r"\b(?:[Pp]atient|Mr\.?|Mrs\.?|Ms\.?)\s+(?!Portal\b|Records?\b|ID\b)[A-Z][a-z]+")
_DATA_NOUN = re.compile(
    r"\b(?:records?|charts?|files?|information|info|data|details|history|diagnos\w*|results?|"
    r"medications?|treatments?|conditions?|lab|labs|address|phone|email|age|name|names|list)\b"
)

_PRESCRIPTION = (
    re.compile(r"\bprescrib(?:e|ing)\s+(?!means?\b|is\b|are\b|a\s+drug\b)\w+"),
    re.compile(
        r"\b(?:write|give|send|issue|order|renew|refill|approve|authori[sz]e)\s+"
        r"(?:(?:me|us|my|a|an|the|some|another|new)\s+)*"
        r"(?:prescriptions?|refills?|scripts?|medications?|antibiotics?|painkillers?|pills?|meds)\b"
    ),
    re.compile(
        r"\b(?:i need|i want|i would like|can i (?:get|have)|could i (?:get|have)|may i have)\b"
        r".{0,30}\b(?:prescriptions?|refills?)\b"
    ),
)

# Personal dosing questions: a Physician or pharmacist must answer these.
_DOSAGE = (
    re.compile(r"\bhow (?:much|many)\b.{0,40}\b(?:should|can|do|could|may) (?:i|we)\b.{0,20}\b(?:take|use|give|have)\b"),
    re.compile(r"\bwhat (?:dose|dosage|amount)\b.{0,30}\b(?:should|can|do|could) (?:i|we)\b"),
    re.compile(r"\b(?:should|can|could|may) i (?:take|double|skip|stop taking|increase|decrease|combine|mix)\b.{0,40}\b(?:mg|ml|tablets?|pills?|doses?|medicine|medication|meds|ibuprofen|paracetamol|acetaminophen|aspirin|antibiotics?|insulin)\b"),
)

_DIAGNOSIS = (
    re.compile(r"\bdiagnos(?:e|ing)\s+(?:me|my|this|it|him|her)\b"),
    re.compile(r"\b(?:what(?:'s| is)|tell me|give me|show me)\s+my\s+diagnos\w*"),
    re.compile(r"\bdo i have\b"),
    re.compile(r"\bwhat (?:do|could|might|does) i have\b"),
    re.compile(r"\bwhat(?:'s| is) wrong with me\b"),
    re.compile(r"\bam i (?:dying|going to die|sick|ill)\b"),
    re.compile(r"\bis (?:this|it|that) (?:cancer|diabetes|serious|a tumou?r)\b"),
)

_RECORD_CHANGE = re.compile(
    r"\b(?:change|update|edit|modify|overwrite|delete|remove|alter|replace)\b.{0,40}"
    r"\b(?:my|the)\s+(?:medical\s+)?(?:records?|treatments?|medications?|conditions?|diagnos\w*|charts?|age)\b"
)
# Wording that belongs to the reviewed correction flow, which the model path handles.
_CORRECTION_WORDS = re.compile(r"\b(?:correct\w*|fix|request|review|inaccurate|wrong|mistake)\b")


def _normalise(message: str) -> str:
    return " ".join(message.replace("’", "'").casefold().split())


def _other_patient_access(text: str, message: str, own_name: str | None) -> bool:
    """True when the message asks for another person's private information (see third_party_access)."""
    if third_party_access.is_third_party_record_request(message, own_name):
        return True
    other = _OTHER_PERSON.search(text)
    named = _NAMED_PERSON.search(message)
    if not ((other or named) and _DATA_NOUN.search(text)):
        return False
    # An explicit fictional example ("explain asthma using a fictional patient named Alex") names a
    # made-up person and asks for education, not a record. Anything stronger still blocks.
    if (
        third_party_access.is_fictional_framing(message)
        and third_party_access.classify(message, own_name) != "strong"
        and (named or re.fullmatch(r"patients? named", other.group(0)))
    ):
        return False
    return True


def screen(message: str, own_name: str | None = None) -> SafetyVerdict | None:
    """Return a fixed verdict for a message that must not reach the model, else None.

    own_name is the signed-in patient's own name (from the authenticated session). It only stops the
    patient's own name from being read as a third party; it never selects whose data is read.
    """
    text = _normalise(message)
    if not text:
        return None

    if _EMERGENCY_ALWAYS.search(text) or (
        _EMERGENCY_SYMPTOM.search(text) and _PERSON_REF.search(text)
    ):
        return SafetyVerdict("emergency", "Escalate", "emergency_detected", EMERGENCY_REPLY, "notice")

    if _other_patient_access(text, message, own_name):
        return SafetyVerdict(
            "other_patient_access", "Block", "other_patient_access", OTHER_PATIENT_REPLY, "refusal"
        )

    if any(pattern.search(text) for pattern in _APPROVAL_BYPASS):
        return SafetyVerdict(
            "approval_bypass", "Block", "approval_bypass_not_permitted", APPROVAL_BYPASS_REPLY, "refusal"
        )

    if any(pattern.search(text) for pattern in _PRESCRIPTION):
        return SafetyVerdict(
            "prescription_request", "Block", "prescription_not_permitted", PRESCRIPTION_REPLY, "refusal"
        )

    if any(pattern.search(text) for pattern in _DOSAGE):
        return SafetyVerdict(
            "dosage_request", "Block", "dosage_not_permitted", DOSAGE_REPLY, "refusal"
        )

    if any(pattern.search(text) for pattern in _DIAGNOSIS):
        return SafetyVerdict(
            "diagnosis_request", "Block", "diagnosis_not_permitted", DIAGNOSIS_REPLY, "refusal"
        )

    if _RECORD_CHANGE.search(text) and not _CORRECTION_WORDS.search(text):
        return SafetyVerdict(
            "record_change_request", "Block", "direct_record_change", RECORD_CHANGE_REPLY, "refusal"
        )

    return None


# --- Reading back the patient's OWN recorded details (a lookup, never a diagnosis) ----------

_OWN_RECORD = (
    re.compile(
        r"\bmy (?:current |medical |recorded |health |latest |existing )*"
        r"(?:conditions?|treatments?|records?|age)\b"
    ),
    re.compile(r"\bwhat (?:am i|was i|i am) (?:being )?treated for\b"),
    re.compile(r"\bwhat (?:treatment|condition)s? (?:am i|do i|have i been)\b"),
    re.compile(r"\bhow old am i\b"),
)
# Navigation questions keep the pointer reply, correction wording keeps the reviewed flow,
# and anything involving another person is never a self lookup.
_NOT_SELF_LOOKUP = re.compile(
    r"\b(?:where|how (?:can|do|could|would|to)|correct\w*|fix|wrong|inaccurate|mistake|request|(?!(?:what|that|there|here|it|let|who|how|where|when|why|which)'s)[a-z]{2,}'s|"
    r"dr|doctor|physician|nurse|technician|admin\w*|someone|anyone|anybody|who|they|share|"
    r"explain how|what does the|page|button|tab|section)\b"
)


def is_navigation_or_third_party(message: str) -> bool:
    """True for wording about where/how to find things, corrections, or other people.

    A model-classified "view my record" is read back only when this is False; otherwise the
    patient gets the pointer to My Medical Records (or the correction flow) as before.
    """
    return bool(_NOT_SELF_LOOKUP.search(_normalise(message)))


def is_own_record_query(message: str) -> bool:
    """True for a plain request to read back the patient's own recorded details.

    Call only after screen() returned None, so emergencies, other-person access, prescription,
    dosage, diagnosis and direct-change requests have already been handled.
    """
    text = _normalise(message)
    if not text or _NOT_SELF_LOOKUP.search(text):
        return False
    return any(pattern.search(text) for pattern in _OWN_RECORD)
