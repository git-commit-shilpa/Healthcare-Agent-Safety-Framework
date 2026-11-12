"""Service-request categories. Must match the CHECK list in migration 010 (a test asserts this)."""

ROUTINE_CATEGORIES = {
    "appointment_scheduling": "Appointment scheduling",
    "test_result_upload": "Test result upload",
    "sample_collection": "Sample collection",
    "supplies_equipment": "Supplies or equipment",
    "documentation_request": "Documentation request",
    "general_inquiry": "General question",
}
# Always need a Physician. A Technician can never authorize these on their own.
CLINICAL_CATEGORIES = {
    "medication_request": "Medication request",
    "treatment_change_request": "Treatment change request",
    "diagnosis_inquiry": "Diagnosis question",
}
ALL_CATEGORIES = {**ROUTINE_CATEGORIES, **CLINICAL_CATEGORIES}

# Medical-record correction coordination (migration 014). NOT part of ALL_CATEGORIES: a Patient can never choose it
# in the service-request form; it is created only together with a correction request.
CORRECTION_CATEGORY = "record_correction"
CORRECTION_CATEGORY_LABEL = "Record correction"

# The only reasons a Technician may DENY a correction. All administrative: never a clinical judgement. The database
# accepts exactly these codes (decline_<code>), and only while no Physician approval exists.
DECLINE_REASONS = {
    "duplicate_request": "Duplicate of a request already on file",
    "incomplete_information": "Information requested but not provided",
    "withdrawn_by_patient": "Withdrawn by the patient",
    "not_a_correction_request": "Not a record correction request",
}

TERMINAL_STATUSES = ("Completed", "Declined", "Cancelled")
ALL_STATUSES = (
    "Submitted", "In Review", "On Hold", "Awaiting Physician",
    "In Progress", "Completed", "Declined", "Cancelled",
)

# Decision -> statuses it may be taken from (mirrors record_technician_task_decision).
DECISION_FROM_STATUSES = {
    "ALLOW": ("In Review", "Awaiting Physician"),
    "BLOCK": ("In Review", "On Hold", "Awaiting Physician"),
    "HOLD": ("In Review",),
}

DEFAULT_ALLOW_PATIENT_MESSAGE = "Your request has been accepted and is being handled by your care team."
DEFAULT_COMPLETED_PATIENT_MESSAGE = "Your request has been completed."


def is_clinical(category: str) -> bool:
    # A record correction always needs the Physician (015), although it is not a patient-submittable category.
    return category in CLINICAL_CATEGORIES or category == CORRECTION_CATEGORY
