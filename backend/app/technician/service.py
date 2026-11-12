"""Technician data access. All reads are scoped by patient_assignments."""

from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import CurrentUser, _insert_chat_audit, _patient_service_rows
from app.technician.schemas import AssignedPatient

PATIENTS_UNAVAILABLE = "Assigned patient records are temporarily unavailable."
PATIENT_NOT_FOUND = "Patient not found."
_PATIENT_COLUMNS = "patient_id,user_id,age,medical_condition,treatment"


def _to_patient(row: dict[str, Any], names: dict[int, str]) -> AssignedPatient:
    try:
        return AssignedPatient.model_validate({**row, "name": names.get(row.get("user_id"))})
    except ValidationError as error:
        raise HTTPException(status_code=502, detail=PATIENTS_UNAVAILABLE) from error


async def _assigned_patient_ids(user: CurrentUser) -> list[int]:
    rows = await _patient_service_rows(
        "patient_assignments",
        {"select": "patient_id", "staff_user_id": f"eq.{int(user.user_id)}"},
        unavailable_detail=PATIENTS_UNAVAILABLE,
    )
    ids: set[int] = set()
    for row in rows:
        patient_id = row.get("patient_id")
        if isinstance(patient_id, int) and not isinstance(patient_id, bool):
            ids.add(patient_id)
    return sorted(ids)


async def _patient_names(user_ids: list[int]) -> dict[int, str]:
    """Names come from users via patients.user_id. Call only AFTER the assignment check."""
    clean = sorted({i for i in user_ids if isinstance(i, int) and not isinstance(i, bool)})
    if not clean:
        return {}
    rows = await _patient_service_rows(
        "users",
        {"select": "user_id,name", "user_id": f"in.({','.join(str(i) for i in clean)})"},
        unavailable_detail=PATIENTS_UNAVAILABLE,
    )
    return {
        r["user_id"]: r["name"]
        for r in rows
        if isinstance(r.get("user_id"), int) and isinstance(r.get("name"), str)
    }


async def list_assigned_patients(user: CurrentUser) -> list[AssignedPatient]:
    patient_ids = await _assigned_patient_ids(user)
    if not patient_ids:
        return []
    rows = await _patient_service_rows(
        "patients",
        {
            "select": _PATIENT_COLUMNS,
            "patient_id": f"in.({','.join(str(i) for i in patient_ids)})",
            "order": "patient_id.asc",
        },
        unavailable_detail=PATIENTS_UNAVAILABLE,
    )
    allowed = set(patient_ids)
    # Defence in depth: never return a row outside the assignment set.
    rows = [r for r in rows if r.get("patient_id") in allowed]
    names = await _patient_names([r.get("user_id") for r in rows])
    return [_to_patient(r, names) for r in rows]


async def get_assigned_patient(user: CurrentUser, patient_id: int) -> AssignedPatient:
    """Return one assigned patient; unassigned and unknown ids are indistinguishable."""
    assignment = await _patient_service_rows(
        "patient_assignments",
        {
            "select": "patient_id",
            "staff_user_id": f"eq.{int(user.user_id)}",
            "patient_id": f"eq.{int(patient_id)}",
            "limit": "1",
        },
        unavailable_detail=PATIENTS_UNAVAILABLE,
    )
    if not assignment:
        await _insert_chat_audit(
            user, "technician.patient.access_denied", {"patient_id": int(patient_id)}
        )
        raise HTTPException(status_code=404, detail=PATIENT_NOT_FOUND)

    rows = await _patient_service_rows(
        "patients",
        {
            "select": _PATIENT_COLUMNS,
            "patient_id": f"eq.{int(patient_id)}",
            "limit": "1",
        },
        unavailable_detail=PATIENTS_UNAVAILABLE,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=PATIENT_NOT_FOUND)
    patient = _to_patient(rows[0], await _patient_names([rows[0].get("user_id")]))
    await _insert_chat_audit(
        user, "technician.patient.viewed", {"patient_id": patient.patient_id}
    )
    return patient
