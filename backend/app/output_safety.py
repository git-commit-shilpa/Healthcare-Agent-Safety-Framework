"""Deterministic safety check for generated health-education replies.

Why this exists: the local 3B model that NeMo uses as an "output judge" is not reliable
(measured on labelled replies it blocked most harmless explanations and still missed unsafe
ones). For the general-health-education route the output gate is therefore a fixed,
category-based check that can be reviewed and tested. It looks for the specific harms that
route must never produce, so a plain explanation of what a condition is passes, while text
that diagnoses the reader, doses or prescribes, changes a record, bypasses a Physician,
exposes a person's details or leaks instructions is stopped.

It returns only a category name (never the matched text) so it is safe to log and audit.
"""

import re
from dataclasses import dataclass

_I = re.IGNORECASE

_CONDITIONS = (
    r"(?:type \d |chronic |acute |mild |severe )?(?:hypertension|high blood pressure|low blood pressure|"
    r"diabetes|asthma|cancer|tumou?r|flu|influenza|cold|infection|virus|disease|disorder|syndrome|"
    r"condition|anaemia|anemia|pneumonia|bronchitis|covid\S*|migraine|depression|arthritis|"
    r"heart failure|stroke|allergy|copd)"
)
_NOT_AFTER = r"(?<!\bif )(?<!\bwhen )(?<!\bwhether )(?<!\bthat )(?<!\bunless )(?<!\bas )"

CATEGORIES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "personal_diagnosis",
        (
            re.compile(
                _NOT_AFTER + r"\byou (?:probably |likely |may well |most likely |definitely |clearly |already |"
                r"might |may |could )?(?:have|has|are suffering from|suffer from|are experiencing|developed|"
                r"are diagnosed with) (?:a |an |the )?" + _CONDITIONS + r"\b",
                _I,
            ),
            re.compile(
                r"\b(?:based on|given|from) your (?:symptoms|description|answers)\b|your diagnosis is|"
                r"you (?:are|were) diagnosed|\bi diagnose\b|it sounds like you|sounds like you (?:have|might|may)",
                _I,
            ),
        ),
    ),
    (
        "clinician_claim",
        (re.compile(r"\bas (?:your|a) (?:doctor|physician|nurse|clinician)\b|\bi am (?:a |your )?(?:doctor|physician|nurse|clinician)\b", _I),),
    ),
    (
        "dose_or_prescription",
        (
            re.compile(r"\bi prescribe\b|\bi recommend (?:taking|that you take)\b", _I),
            re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|ml|iu)\b", _I),
            re.compile(r"\btake \d+\b|\b(?:tablets?|pills?|capsules?) (?:twice|three times|daily|every|per day|a day)\b", _I),
        ),
    ),
    (
        "personalised_treatment",
        (
            re.compile(
                r"\byou (?:should|must|need to|ought to) (?:stop|start|switch|double|increase|decrease|reduce|"
                r"skip|quit|begin|try taking|take)\b(?! a (?:rest|break|look|walk|moment))(?! (?:care|it easy|time))",
                _I,
            ),
            re.compile(
                r"\b(?:stop|double|increase|reduce|skip|switch|change|adjust|halve|up) (?:taking )?your "
                r"(?:\w+ ){0,3}(?:medic\w+|insulin|tablets?|pills?|dose|dosage|treatment|prescription|inhaler)\b",
                _I,
            ),
        ),
    ),
    (
        "record_change_claim",
        (
            re.compile(
                r"\bi(?:'ve| have)? (?:just |now |already )?(?:updated|changed|approved|submitted|modified|"
                r"deleted|edited|amended|rejected) (?:your|the) (?:record|treatment|medication|request|"
                r"prescription|chart|condition)\b",
                _I,
            ),
            re.compile(r"\b(?:your|the) (?:record|treatment|request) (?:has been|is now|was) (?:updated|changed|approved|submitted)\b", _I),
        ),
    ),
    (
        "approval_bypass",
        (
            re.compile(
                r"\bwithout (?:waiting for |asking |consulting |seeing |the approval of |your )?"
                r"(?:your |a |the )?(?:physician|doctor)\b",
                _I,
            ),
            re.compile(
                r"\bno need (?:to|for) (?:see|ask|consult|wait for|involve) (?:a |your |the )?(?:physician|doctor)\b|"
                r"\b(?:bypass|bypassing|skip|skipping) (?:the |your )?(?:physician|doctor|review|approval)\b",
                _I,
            ),
        ),
    ),
    (
        "privacy",
        (
            re.compile(r"\bborn\b.{0,20}\d|date of birth|\bdob\b|\blives? (?:at|on|in)\b", _I),
            re.compile(r"\b\d+\s+\w+\s+(?:street|st|road|rd|avenue|ave|lane|drive)\b", _I),
            re.compile(r"\banother (?:patient|person|user|individual)\b|\bpatient (?:named|called|id)\b", _I),
            re.compile(r"[\w.+-]+@[\w-]+\.\w+|\b\d{3}-\d{2}-\d{4}\b|\+?\d[\d\s().-]{8,}\d", _I),
        ),
    ),
    (
        "instruction_leak",
        (re.compile(r"\bsystem (?:prompt|instructions)\b|\bmy (?:hidden |secret )?instructions\b|\breveal (?:the |this |my )?(?:prompt|instructions)\b", _I),),
    ),
)


@dataclass(frozen=True)
class OutputCheck:
    passed: bool
    category: str | None = None


# Advice such as "never stop taking your medication without consulting your doctor" is safe.
# For these categories a match inside a negated sentence is ignored.
_NEGATION_AWARE = frozenset({"personalised_treatment", "approval_bypass"})
_NEGATION = re.compile(
    r"\b(?:do not|don't|dont|never|not|avoid|shouldn't|should not|cannot|can't|won't|must not|mustn't|"
    r"no one should|nobody should)\b",
    _I,
)
_SENTENCE_END = re.compile(r"[.!?\n]")


def _negated(text: str, start: int) -> bool:
    boundaries = [m.end() for m in _SENTENCE_END.finditer(text, 0, start)]
    prefix = text[(boundaries[-1] if boundaries else 0):start]
    return bool(_NEGATION.search(prefix))


def check(text: str) -> OutputCheck:
    """Pass a plain educational reply; fail with the first violated category."""
    for category, patterns in CATEGORIES:
        for pattern in patterns:
            for match in pattern.finditer(text):
                if category in _NEGATION_AWARE and _negated(text, match.start()):
                    continue
                return OutputCheck(False, category)
    return OutputCheck(True)
