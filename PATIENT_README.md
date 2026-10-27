# Patient Module

This document describes the Patient authentication and dashboard foundation and
the read-only medical-record feature currently implemented in the Healthcare
Agent Safety Framework (HASF). For general project setup, Supabase account
linking, and shared application instructions, see [README.md](./README.md).

## Implemented features

- Uses the application's existing Supabase Auth email/password sign-in,
  session restoration, and sign-out behavior; there is no separate Patient
  authentication system.
- Verifies the Supabase access token through FastAPI and resolves the linked
  application account and role from `public.users`.
- Routes authenticated Patients to `/dashboard/patient`.
- Shows the authenticated Patient's name in the dashboard welcome area.
- Provides navigation for Overview, My Medical Records, Ask AI Assistant,
  AI Request History, and My Profile. Sections that are not implemented yet
  are identified as placeholders.
- Loads the signed-in Patient's own read-only medical-record summary from
  Supabase, with loading, empty, error, and retry states.
- Uses Patient-specific styling without changing the Admin dashboard design.

## Authentication and session behavior

The Patient signs in using the same Supabase Auth email/password flow as the
other application roles. The frontend sends the access token to FastAPI.
FastAPI validates it with Supabase Auth, finds the corresponding linked row in
`public.users`, and uses the database-derived role and active status. An
unlinked account, inactive account, or account whose database role is not
Patient is rejected.

The authenticated-user response includes the Patient's name from
`public.users`; it does not expose additional personal information for the
dashboard. A Patient's existing session persists across reloads in the
current tab's session storage and ends on sign-out or when the tab is closed.
Patients are not subject to the Admin-only 10-minute inactivity timeout.

Frontend route protection is for navigation and user experience; it is not the
authorization boundary. FastAPI independently enforces the Patient role on
Patient endpoints. A Patient cannot use the Patient role to access Admin,
Physician, or Technician role dashboards or Admin endpoints.

## Dashboard

The dashboard route is `/dashboard/patient`. Its navigation sections are:

| Section | Current behavior |
| --- | --- |
| Overview | Patient dashboard landing section and welcome area |
| My Medical Records | Fetches and displays the authenticated Patient's own record |
| Ask AI Assistant | Clearly identified placeholder; no Patient AI feature is implemented |
| AI Request History | Clearly identified placeholder; no Patient request-history feature is implemented |
| My Profile | Clearly identified placeholder; profile editing is not implemented |

No sample or invented medical record is embedded in React. The record view
shows the data returned by the backend, a safe empty message when no record is
linked, and an error with a retry option if loading fails.

## Supabase medical records

The feature reuses the existing `public.patients` table and does not add or
change tables, migrations, or permissions. The backend resolves the application
`user_id` from the authenticated account and uses that server-derived value to
query `public.patients`. The frontend does not submit a patient ID or user ID.

The response contains only:

- `age`
- `medical_condition`
- `treatment`

Changing the Patient's row in Supabase is reflected the next time the dashboard
fetches the record (for example, by revisiting the section or retrying after an
error). A missing linked record is returned as `{"record": null}`. If multiple
records match the authenticated account, the API fails safely rather than
choosing one.

## Patient API endpoints

| Method and path | Purpose | Authorization |
| --- | --- | --- |
| `GET /api/auth/me` | Returns the authenticated application's user information, including the Patient's name | Valid Supabase access token and valid linked active account |
| `GET /api/dashboard/patient` | Provides the Patient role dashboard response | Valid authentication and database-derived Patient role |
| `GET /api/patient/my-record` | Returns the authenticated Patient's own record summary or `{"record": null}` | Valid authentication and database-derived Patient role; ownership is resolved server-side |
| `GET /api/health` | Reports application/database health | Health check endpoint |

`GET /api/patient/my-record` returns HTTP 401 for unauthenticated requests and
HTTP 403 for authenticated non-Patient accounts. A duplicate record match
returns HTTP 409. Invalid Supabase record data returns a safe HTTP 502
response, and database/transport failures return a safe HTTP 503 response;
these responses do not include credentials or unrelated medical information.
Consult `/docs` on the running backend for the current API schema.

## Security

- Supabase Auth verifies the access token; FastAPI determines the application
  role from the linked `public.users` row.
- A user must have a valid linked, active account. The frontend cannot choose
  the role used for authorization.
- Patient record ownership is enforced by the backend using the authenticated
  account's `public.users.user_id`. Client-supplied IDs are not used to select
  a record.
- The endpoint selects and returns only `age`, `medical_condition`, and
  `treatment`. It does not expose the record's `patient_id` or `user_id`.
- The server-side Supabase service-role credential, if configured for backend
  reads, must remain in the backend environment and must never be exposed to
  the browser.
- Record access is read-only. This feature does not edit records, create
  correction requests, or implement an AI or approval workflow.
- Continue to use fictional healthcare data in development and testing.

## Setup

The Patient module uses the same frontend, backend, and Supabase project as the
rest of HASF; it does not require separate Patient credentials, schema changes,
or additional packages. Follow the shared installation, environment-file, and
Supabase Auth configuration instructions in [README.md](./README.md), then
start the backend and frontend as described there.

For Patient access, verify that:

1. Email/password authentication is enabled in the existing Supabase project.
2. The Supabase Auth account is linked to the intended `public.users` row.
3. That row has the `Patient` role and is active.
4. The Patient's `public.users.user_id` has the corresponding row in
   `public.patients`.
5. Backend Supabase credentials are configured only in the backend
   environment, not in frontend variables.

No Patient-specific SQL migration is required.

## Focused backend tests

Run these commands from the `backend` directory:

```powershell
python -m unittest discover -s tests -p "test_patient_authorization.py" -v
python -m unittest discover -s tests -p "test_patient_records.py" -v
```

The authorization tests cover Patient authentication/name resolution, invalid
linked accounts, Patient access to the Patient dashboard, and rejection of
Admin, Physician, and Technician roles from Patient-only routes. The medical
record tests cover unauthenticated and non-Patient access, server-side
ownership filtering, successful retrieval, missing and duplicate records, and
safe database/transport errors.

The frontend package currently defines development, build, and preview
scripts, but no dedicated frontend test script or Patient component test suite.

## Limitations and future development

The current Patient scope is dashboard navigation plus read-only viewing of a
Patient's own medical-record summary. The following are not implemented:

- Editing or submitting corrections to a medical record.
- Physician review, approval, or rejection workflows.
- Patient-facing AI interaction or AI request history.
- Patient profile editing.
- Medical-record creation or retrieval by client-selected identifiers.
- Dedicated frontend Patient component tests.

Any future correction or AI workflow should be implemented as a separate
approved phase. Keep identity, role, ownership, assignment, approval, and
database authorization checks deterministic and server-enforced; do not allow
the frontend or an AI model to authorize record access or modify clinical
records directly.
