"""Deterministic detector for a Patient asking for ANOTHER identifiable person's private information.

Why this exists: whether a request targets someone else's record must never depend on a language
model's refusal. This runs on the raw message before any record lookup or model call, and it is only
one layer: the intent classifier has an explicit ``other_patient_access`` intent, and the Action
Gateway blocks it. Record reads are always keyed on the authenticated session, never on a name in
the message, and this module never touches the database, so a reply can't reveal whether a named
person exists.

It is structural, not a name list. A request is third-party private-record access when the message
(1) refers to a person who is not the signed-in patient, (2) in a way that asks for health or record
information about that person. General education ("What is hypertension?", "Explain diabetes using a
fictional patient called Alex") is not matched, because no private record is requested.

Signals, strongest first:
  STRONG (never exempted): a possessive or "of/for/about <Name>" next to a record or health term, a
  question about what a named person has/takes, "another patient", someone else's data, relatives and
  acquaintances' data, patient ids.
  SOFT (exempted only inside an explicit fictional/example framing): "patient named X", "Mr. X",
  a declarative "X has diabetes".
"""

import re

# Record or health information: what makes a mention of a person a *record* request.
_RECORD_TERM = (
    r"(?:records?|charts?|files?|information|info|data|details|history|histories|diagnos\w*|results?|"
    r"medic\w*|drugs?|prescri\w+|treat\w*|conditions?|illness\w*|disease\w*|diabet\w*|allerg\w*|"
    r"surger\w*|lab|labs|scans?|vitals?|age|address|phone|email|status|health|condition|symptoms?|"
    r"ail\w+|sick|ill|pregnan\w*|hiv|cancer|admitted|hospitali[sz]\w*|dosages?|doses?)"
)
_RECORD_TERM_RE = re.compile(rf"\b{_RECORD_TERM}\b", re.IGNORECASE)

# Capitalised words that are NOT people: portal vocabulary, roles, calendar words and common conditions.
_NOT_A_PERSON = frozenset(
    """
    i i'm my me we you your the a an this that these those it its in on of for about from with and or but
    if is are was were do does did can could should would will may might what which who whom whose when where
    why how please show tell give get find fetch display pull open check view list explain describe share send
    hello hi hey thanks thank yes no ok okay
    hasf portal overview profile records record request requests history assistant ai help page dashboard
    medical health medication medications treatment treatments condition conditions diagnosis report reports
    physician physicians technician technicians admin administrator doctor doctors nurse nurses patient patients
    care team correction corrections review approval status appointment appointments schedule service services
    monday tuesday wednesday thursday friday saturday sunday january february march april may june july august
    september october november december today tomorrow yesterday
    diabetes hypertension asthma cancer arthritis anemia anaemia migraine epilepsy depression anxiety obesity
    pneumonia bronchitis influenza flu covid stroke dementia alzheimer alzheimers parkinson parkinsons
    cholesterol osteoporosis eczema psoriasis hepatitis tuberculosis malaria dengue measles asthma copd
    insulin aspirin ibuprofen paracetamol acetaminophen metformin penicillin amoxicillin statin statins
    vitamin vitamins antibiotic antibiotics
    heart blood kidney liver lung lungs brain chest skin bone bones eye eyes ear ears back head stomach thyroid
    prostate breast high low common early late side effects signs normal general healthy mental chronic acute
    type symptoms causes risk risks long short new old
    """.split()
)
# Medical-term suffixes (conditions/procedures) so "Gastritis" or "Neuropathy" is not read as a name.
_MEDICAL_SUFFIX = re.compile(r"(?:itis|osis|emia|aemia|pathy|oma|algia|plegia|trophy|ectomy|otomy|plasty|scopy)$", re.IGNORECASE)

_NAME = r"[A-Z][a-z]{1,}(?:[ -][A-Z][a-z]{1,})?"
_TITLE = r"(?:(?:Mr|Mrs|Ms|Miss|Mx|Dr)\.?\s+)?"

# --- STRONG signals -----------------------------------------------------------------------------

_OTHER_PERSON_WORDS = re.compile(
    r"\b(?:another|other|different) (?:patients?|people|persons?|users?|individuals?)\b"
    r"|\b(?:someone|somebody|anyone|anybody|everyone|everybody) else'?s?\b"
    r"|\b(?:all|every|each|any) (?:the )?(?:other )?patients?\b"
    r"|\b(?:their|his|her) (?:medical |health )?(?:records?|conditions?|diagnos\w+|medications?|treatments?|history|results?|information)\b"
    r"|\bmy (?:wife|husband|mother|father|mom|dad|friend|neighbou?r|son|daughter|brother|sister|"
    r"colleague|coworker|boss|ex|partner|girlfriend|boyfriend|cousin|uncle|aunt|grand\w+)'?s?\b"
    r"(?=.{0,40}" + _RECORD_TERM + r")",
    re.IGNORECASE,
)
# Patient ids and numbered patients: never a legitimate self-service request.
_PATIENT_ID = re.compile(
    r"\bpatient[\s_-]*(?:id|no\.?|number|#)?[\s:=#-]*\d+\b|\b(?:patient_id|patientid|user_id|userid)\b\s*[:=]?\s*\d*",
    re.IGNORECASE,
)
# "Praveen's condition", "Praveen's"
_POSSESSIVE_NAME = re.compile(rf"\b({_NAME})['’]s\b")
# "condition of Praveen", "records for Dr. Rao", "details about Maria Lopez"
_OF_NAME = re.compile(rf"\b(?i:of|for|about|regarding|concerning|on|belonging to)\s+{_TITLE}({_NAME})\b")
# "show me Praveen", "get Praveen medical condition", "look up Maria", "pull up Rao"
_LOOKUP_NAME = re.compile(
    rf"\b(?i:show|tell|give|get|find|fetch|display|pull(?: up)?|open|check|view|look(?: up)?|access|retrieve|read|list|see)\b"
    rf"(?i:\s+(?:me|us))?(?i:\s+(?:the|a))?\s+{_TITLE}({_NAME})\b"
)
# "Praveen medical condition", "Praveen medications"
_NAME_THEN_RECORD = re.compile(rf"\b({_NAME})\s+(?i:(?:medical\s+|health\s+)?{_RECORD_TERM})\b")
# Interrogative about a named person's status: "what medications does Praveen take", "is Praveen diabetic",
# "does Maria have cancer", "how is Praveen's health".
_ASKS_ABOUT_NAME = re.compile(
    rf"\b(?i:what|which|how|is|are|was|were|does|did|do|has|have|when|where|why)\b[^.?!]{{0,40}}?"
    rf"\b(?i:is|are|was|were|does|did|do|has|have|had|takes?|taking|suffers?|got)\s+{_TITLE}({_NAME})\b"
    rf"|\b(?i:is|are|was|were|does|did|has|have)\s+{_TITLE}({_NAME})\b"
)

# --- SOFT signals (exempt when the message is explicitly a fictional example) ---------------------

_NAMED_PATIENT = re.compile(
    rf"\b(?i:patients?|person|man|woman|boy|girl|someone|individual)\s+(?i:named|called|name is)\s+({_NAME})\b"
    rf"|\b(?i:patient|Mr|Mrs|Ms|Miss|Mx)\.?\s+({_NAME})\b"
)
_DECLARATIVE_HEALTH = re.compile(
    rf"\b({_NAME})\s+(?i:has|have|had|takes?|is taking|suffers?|was diagnosed|is diagnosed|is on)\b"
)

# Explicit example framing. "hypothetically" alone is deliberately NOT here: it is a common way to
# disguise a real request.
_FICTIONAL = re.compile(
    r"\b(?:fictional|fictitious|imaginary|invented|made[- ]up|pretend|sample patient|example patient|"
    r"case study|for example|for instance|e\.g\.|imagine|suppose|let'?s say|textbook example|test patient)\b",
    re.IGNORECASE,
)



def _normalise(message: str) -> str:
    return message.replace("’", "'")


def _own_tokens(own_name: str | None) -> frozenset[str]:
    if not own_name:
        return frozenset()
    return frozenset(part.casefold() for part in re.split(r"[\s.,_-]+", own_name) if len(part) > 1)


def _looks_like_person(token: str, own: frozenset[str], message: str, start: int) -> bool:
    """A capitalised word that could be a person's name: not portal/medical vocabulary, not the user."""
    parts = re.split(r"[ -]", token)
    for part in parts:
        folded = part.casefold()
        if folded in _NOT_A_PERSON or _MEDICAL_SUFFIX.search(folded):
            return False
    if any(part.casefold() in own for part in parts):
        return False  # the signed-in patient's own name; the lookup is keyed on the session anyway
    return True


def _names(pattern: re.Pattern, message: str, own: frozenset[str]) -> list[str]:
    found = []
    for match in pattern.finditer(message):
        for index in range(1, (pattern.groups or 0) + 1):
            token = match.group(index)
            if token and _looks_like_person(token, own, message, match.start(index)):
                found.append(token)
    return found


def classify(message: str, own_name: str | None = None) -> str | None:
    """Return "strong", "soft" or None for the strongest third-party signal in the message.

    A signal exists only when a person other than the signed-in patient is referred to in a way that
    asks for (or states) health or record information about them.
    """
    text = _normalise(message or "")
    if not text.strip():
        return None
    own = _own_tokens(own_name)
    has_record_term = bool(_RECORD_TERM_RE.search(text))

    if _PATIENT_ID.search(text):
        return "strong"
    if has_record_term and _OTHER_PERSON_WORDS.search(text):
        return "strong"
    if has_record_term and _names(_POSSESSIVE_NAME, text, own):
        return "strong"
    if has_record_term and _names(_OF_NAME, text, own):
        return "strong"
    if has_record_term and _names(_NAME_THEN_RECORD, text, own):
        return "strong"
    if has_record_term and _names(_LOOKUP_NAME, text, own):
        return "strong"
    if has_record_term and _names(_ASKS_ABOUT_NAME, text, own):
        return "strong"

    soft = bool(
        has_record_term and (_names(_NAMED_PATIENT, text, own) or _names(_DECLARATIVE_HEALTH, text, own))
    )
    return "soft" if soft else None


def is_fictional_framing(message: str) -> bool:
    """True for an explicit made-up example ("fictional patient", "for example", "imagine")."""
    return bool(_FICTIONAL.search(message or ""))


def is_third_party_record_request(message: str, own_name: str | None = None) -> bool:
    """True when the message asks for another identifiable person's private health information."""
    signal = classify(message, own_name)
    if signal == "strong":
        return True
    if signal == "soft":
        # A plainly fictional/example framing ("Explain diabetes using a fictional patient named Alex")
        # asks for education, not a private record.
        return not _FICTIONAL.search(message)
    return False
