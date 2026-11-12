# Physician integration contract: `technician_task_approval`

For the developer of the Physician module. **Status: proposed, not yet implemented on the Technician side.**
The Technician module will NOT expose any approve/reject endpoint; resolving these approvals is
**owned entirely by the Physician module**. We reuse the shared `ai_requests` + `approvals` tables.

## 1. How to recognise a Technician escalation
A Technician "HOLD for physician approval" creates, in one transaction:

| Table | Row |
|---|---|
| `ai_requests` | `action = 'technician_task_approval'`, `guardrail_decision = 'Escalate'`, `decision_reason = 'Human approval required'`, `requested_by` = the Technician, `patient_id` = the patient |
| | `request_details` = JSON text `{"task_id": <int>, "category": "<category>", "requested_action": "physician_approval_for_task"}`. **No free text, no patient name.** |
| `approvals` | exactly **one** row: `request_id` = that request, `reviewer_id` = the patient's single active assigned Physician holding `review_escalation`, `status = 'Pending'`, `reviewed_at = NULL` |
| `audit_logs` | `technician.task.decision.hold` (no notes) |

The task is `technician_tasks.approval_request_id = ai_requests.request_id`.

## 2. What the Physician module must do to resolve one
Do it in **one transaction** (an RPC like the existing correction RPCs is recommended) using the
service-role key, with the Physician's id taken from the verified FastAPI auth context, never from the request body:

1. Caller is an **active Physician**; `permissions` has `Physician / review_escalation / allowed = true`.
2. Caller is `approvals.reviewer_id` for that row **and still assigned** to the patient in `patient_assignments`.
3. `ai_requests.action = 'technician_task_approval'` and `ai_requests.requested_by <> caller`
   (a requester can never resolve their own escalation).
4. Compare-and-set so a decision can be made only once:
   `update approvals set status = 'Approved' | 'Rejected', reviewed_at = now() where approval_id = :id and status = 'Pending' and reviewer_id = :caller`.
   Zero rows updated = already resolved: return 409.
5. Write an audit row (suggested `physician.approval.approved` / `physician.approval.rejected`, `request_id` set, **no clinical free text**).

Exact vocabulary: `status` is `Pending`, `Approved` or `Rejected` (capitalised). Always set `reviewed_at` when resolving.

## 3. What the Physician module must NOT do
* Do not change `technician_tasks` (status, decision, messages) or `technician_task_events`. The Technician side
  reads the approval live; the task deliberately stays `Awaiting Physician` until the Technician acts.
* Do not edit `action`, `patient_id`, `request_details` or `requested_by` of the request, add a second
  `approvals` row, or reassign `reviewer_id` after creation. The Technician side treats any of these as an
  **invalid approval** and refuses to proceed.
* Do not create `technician_task_approval` requests. Only the Technician RPC creates them.

## 4. Guarantees the Technician side enforces (so you do not have to)
An approval counts as **granted** only if, at the moment the Technician acts (ALLOW, and again at completion):
exactly one approvals row exists; `status = 'Approved'` with `reviewed_at` set; the request is for **this
patient and this task** and this action; the reviewer is an **active Physician, still assigned to the patient,
still holding `review_escalation`, and is not the Technician**. A rejected, pending, tampered or revoked
approval blocks the Technician. Approval never grants the Technician a clinical permission (their
`update_medical_record` stays `false`); it only unblocks the operational task.

## 5. Reading Technician context for review
* Tasks and internal case notes for requests you review are readable under RLS
  (`technician_tasks.approval_request_id` -> `approvals.reviewer_id = you`). Through FastAPI (service role)
  apply the same filter: `approvals.reviewer_id = <caller>`.
* The patient-facing message is separate (`patient_message`); internal notes live only in
  `technician_task_events.internal_note` and must not be shown to Patients or sent to an LLM.

## 6. Open questions for the Physician developer
1. Do you want a single shared approve/reject RPC for all escalation types, or one per action?
2. Notification of new pending approvals (none exists today).
3. If a patient has zero or several eligible Physicians, the Technician HOLD fails with 409
   `no_unique_reviewer` (same rule as the correction flow). Confirm that is acceptable.
4. Live note: both Physician accounts currently have no `auth_user_id` link, so they cannot log in yet.

---

# Part 2: Appointment scheduling (migration 013)

**Status: implemented; the schema is in `database/database_setup.sql`.** The Physician module is not
modified by this work. Everything below lives in new files (`backend/app/scheduling/physician_*.py`). There is deliberately **no Physician UI**: the Physician
module owns that and can call the endpoints below.

## 7. What Physicians can do

Physicians manage **when they can be booked**. They do **not** confirm or reject appointment requests:
routine scheduling confirmation belongs to the assigned Technician. A Physician is involved only when a
Technician uses "Refer to Physician" (the existing `technician_task_approval` flow in Part 1).

| Capability | Endpoint (all under `/api/physician/schedule`) | Database function |
|---|---|---|
| List my weekly working windows | `GET /rules` | table read, filtered by caller |
| Add / edit a window | `POST /rules` (body `rule_id` to edit) | `upsert_schedule_rule` |
| Stop using a window | `POST /rules/{id}/deactivate` | `deactivate_schedule_rule` |
| List my time off / extra openings | `GET /exceptions` | table read, filtered by caller |
| Block a period, or open extra time | `POST /exceptions` | `add_schedule_exception` |
| Remove an exception | `POST /exceptions/{id}/remove` | `remove_schedule_exception` |
| See my own appointments (read only) | `GET /appointments` | table read, filtered by caller |

Request shapes: a **rule** is `{weekday 0-6 (0=Sunday), start_time "HH:MM", end_time "HH:MM", slot_minutes 10-120,
timezone (IANA), effective_from?, effective_to?}` in the doctor's **local** time (the window must divide evenly into
slots). An **exception** is `{kind: "unavailable"|"extra_availability", starts_at, ends_at (ISO with offset),
slot_minutes (extra only), reason?}`. Unknown fields are rejected (422).

## 8. Rules the Physician module must respect
1. **Identity comes from the verified auth context only.** The endpoints never accept a physician id; the RPCs take
   `p_actor_user_id` and verify it is an active Physician holding `Physician / manage_own_schedule = true`.
2. **`manage_own_schedule` is inserted as `allowed = false`.** Nothing works until the database owner sets it to `true` with SQL (the Admin portal's permissions page is read-only). The whole
   feature is also behind `HASF_APPOINTMENTS_ENABLED=true` (which requires `HASF_SERVICE_REQUESTS_ENABLED=true`).
3. A Physician is **bookable** only if: active, `manage_own_schedule` is allowed, and at least one active, unexpired
   rule exists. Patients see only bookable Physicians (`list_bookable_physicians`).
4. Availability is **derived on every request** from rules + exceptions − Confirmed/Completed appointments. Never store
   or cache "available slots", and never let an AI model produce them.
5. All of a Physician's active rules must share **one time zone**; overlapping windows are rejected (409 `rule_overlap`).
6. Changing the schedule **does not cancel** already Confirmed appointments. It can make a *pending* request
   unconfirmable: confirmation re-checks the schedule and returns 409 `slot_unavailable`, and the Technician must reject
   it or ask the patient for another time.
7. Do **not** write to `appointments` directly. Status changes happen only through the existing task RPCs; a trigger
   keeps `appointments.status` in step and performs the final availability check and reservation.
8. Do not send appointment data, patient names or reasons to Ollama or any AI model.

## 9. Data you can rely on
* `appointments`: `appointment_id`, `task_id` (the request id patients see), `patient_id`, `physician_user_id`,
  `starts_at`/`ends_at` (UTC), `physician_timezone`, `status`, `confirmed_by`, `confirmed_at`.
  Statuses: `Pending Confirmation`, `Needs Information`, `Awaiting Physician`, `Confirmed`, `Rejected`, `Cancelled`, `Completed`.
* Only `Confirmed`/`Completed` rows reserve time (two `EXCLUDE` constraints: per physician and per patient).
* Audit events (ids only, never free text): `physician.schedule.rule_saved`, `.rule_deactivated`, `.exception_added`,
  `.exception_removed`, `patient.appointment.requested`, `technician.appointment.confirmed`,
  `technician.appointment.confirm_conflict`, `appointment.status.<status>`.
* `GET /appointments` returns time, status and the **patient's name only**. It intentionally omits the patient's
  free-text reason and all clinical data.

## 10. Open questions for the Physician developer
1. Is showing a Physician the **patient's name** for their own appointments acceptable when that patient is not otherwise
   assigned to them? (Patients may book any bookable Physician; assignment is unchanged.)
2. "Refer to Physician" still uses the Part 1 reviewer rule (exactly one active assigned Physician). Should an appointment
   referral instead go to the **appointment's** Physician? That needs a small change to `record_technician_task_decision`
   (migration 011, also unapplied).
3. Notifications for new/changed appointments do not exist (no notifications table). Portal polling only, for now.
4. Should a Physician be able to cancel a **Confirmed** appointment (for example illness)? Not built: it needs a patient
   notice and a defined cancellation status.
5. Physician accounts still have no `auth_user_id` link, so none of these endpoints can be used live yet.

---

# Part 3: Patient medical-record corrections (Technician-first design, migration 015)

For the developer of the Physician module. **Status: the Patient and Technician sides are built; the Physician portal does not exist
yet and nothing in the database depends on it.** Until it exists, no correction can be approved or rejected in the app, and that is
safe: nothing here changes a medical record without a Physician's decision.

## 3.1 How a correction reaches you
Patient -> **Technician** -> (only if the Technician escalates) Physician.

1. A Patient submits a correction. The database creates **one `ai_requests` row** (`action = 'patient_treatment_correction'`,
   `request_details` = JSON with the proposed text, `guardrail_decision = 'Escalate'`) and **one Technician task**
   (`technician_tasks.category = 'record_correction'`, `approval_request_id` = that request). **No `approvals` row exists yet.**
2. The assigned Technician reviews it and may: ask the Patient for information; **DENY** it for a standard administrative reason
   (duplicate, incomplete after a question, withdrawn, not a correction) while no approval exists; or **HOLD for physician
   authorization**.
3. HOLD for authorization creates **exactly one `approvals` row**: `request_id` = the correction request, `reviewer_id` = the one active
   Physician assigned to the Patient who holds `review_escalation` **and has a linked login**, `status = 'Pending'`, `reviewed_at = NULL`.
   If that Physician cannot sign in (`users.auth_user_id` is null), the escalation is refused and nothing is created.
4. Only now is there something for you to act on.

## 3.2 What the Physician portal must do
1. **List only Pending approvals assigned to the signed-in Physician:** `approvals.status = 'Pending'` AND
   `approvals.reviewer_id = <signed-in Physician>`. The inbox is driven by the `approvals` table.
2. **Never act on an `ai_requests` row that has no approval row.** Such a correction is still with the care team (or was declined by
   it). Do not list it, open it, or decide it. Historical corrections that already have approvals are unchanged and work the same way.
3. **Load correction details only through an authorized backend path** (FastAPI with the service-role key; Python scopes every query).
   Take the Physician id from the verified auth context, never from the request body. Return `request_details` only for a request whose
   approval is assigned to the caller, and only the fields the Physician needs. The proposed text must not go to an LLM, to chat history, to
   logs, or to any other role. (Suggested, not mandated: `GET /api/physician/approvals`, `GET /api/physician/approvals/{approval_id}`,
   `POST /api/physician/approvals/{approval_id}/decision`.)
4. **Decide in one transaction** (an RPC like the existing correction RPCs is recommended), with these checks:
   * caller is an active Physician with `Physician / review_escalation / allowed = true`, is `approvals.reviewer_id`, and is still assigned to the Patient;
   * `ai_requests.action = 'patient_treatment_correction'` for the same Patient as the approval's request;
   * compare-and-set: `update approvals set status = 'Approved' | 'Rejected', reviewed_at = now() where approval_id = :id and status = 'Pending' and reviewer_id = :caller`;
     zero rows updated means it was already decided: return 409;
   * **record `reviewer_id`, `reviewed_at` and the final `status`**. If `reviewed_at` or `reviewer_id` is missing the Technician cannot
     close the coordination task and the decision is treated as invalid by the Technician side;
   * write an audit row (suggested `physician.correction.approved` / `physician.correction.rejected`, `request_id` set, ids only, no clinical text).
5. **Applying an approved change to the medical record is the Physician module's job.** Approving a correction does not modify
   `patients` by itself, and no Technician or Patient code path ever will. If you apply the change, do it in the same transaction as the
   decision, under the Physician's own authority, with its own audit row.
6. Preserve the existing authorization and audit requirements: `Physician / update_assigned_record`, `patient_assignments`, one decision per approval.

## 3.3 What you must not do
* Do not insert `approvals` rows. A trigger refuses any approval for a coordinated correction that does not come from the assigned
  Technician's escalation (it re-validates the actor, task, state, request, Patient and reviewer). Your UPDATE of a Pending row is never intercepted.
* Do not change `technician_tasks`, `technician_task_events`, `ai_requests.request_details`/`patient_id`/`requested_by`, or reassign
  `reviewer_id`. The Technician side reads your decision live and treats tampering as an invalid approval.
* Do not reopen a decided correction: your decision is authoritative. The Technician can only close the coordination task afterwards.

## 3.4 What the database guarantees on your behalf
* One correction -> one request, one task, at most one approval (unique task-to-request link plus the escalation lock).
* The Technician can never ALLOW, approve, reject or apply anything for a correction, and cannot DENY once an approval exists.
* Escalation is refused unless the Physician is active, holds `review_escalation` and has a linked login, so a Pending approval is
  always one you can actually open.
* Historical corrections and their approvals are untouched. A correction with no coordination task keeps the original behaviour.

## 3.5 Acceptance checklist for the Physician portal
* A Pending approval for another Physician is invisible and cannot be decided (404, no hint it exists).
* An `ai_requests` correction with no approval row never appears.
* Deciding twice returns 409 and changes nothing; the final row has `reviewer_id`, `reviewed_at` and a capitalised `status`.
* The proposed text is returned only to the assigned Physician, never logged, never sent to a model.
* After a decision the Patient's Requests page shows Approved / Rejected, and the Technician can close the coordination task.

## 3.6 Open questions for the Physician developer
1. Which RPC applies the approved change to the medical record, and what exactly may it change?
2. Notification of new Pending approvals (none exists yet; Patients and Technicians poll).
3. Reassignment: if the assigned Physician changes after an approval is created, who re-assigns `reviewer_id`? (Today nothing does; the Technician can only escalate once.)
