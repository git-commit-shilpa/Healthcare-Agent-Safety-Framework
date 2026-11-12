# Healthcare Agent Safety Framework (HASF)

A healthcare portal where an AI assistant is kept inside safety rules. Every request is checked by
role-based permissions and a guardrail that decides **ALLOW**, **BLOCK** or **ESCALATE (HOLD)**.
Changes to a medical record are never made by the AI: a Patient's correction request goes to a
Technician first, then to the assigned Physician, who alone can approve it.

Stack: React + Vite frontend, FastAPI backend, Supabase (Auth + PostgreSQL), and a local Ollama model
with NVIDIA NeMo Guardrails for the Patient assistant.

## Roles

| Role | What they can do | Frontend | Backend (`backend/app/`) |
| --- | --- | --- | --- |
| Patient | View their own record, ask the AI assistant, request corrections, service requests and appointments | `frontend/src/modules/patient/` | `main.py` (record, corrections, assistant), `patient_chat.py`, `intent_recognizer.py`, `action_gateway.py`, `safety_rules.py`, `third_party_access.py`, `service_requests/`, `scheduling/` |
| Technician | Review assigned patients' requests, coordinate corrections, confirm appointments | `frontend/src/modules/technician/` | `technician/` |
| Physician | Approve or reject correction requests, set a weekly schedule | not built yet | `scheduling/physician_*`; contract in [PHYSICIAN_INTEGRATION_CONTRACT.md](./PHYSICIAN_INTEGRATION_CONTRACT.md) |
| Admin | Manage users, view permissions, AI request activity and audit logs | `frontend/src/modules/admin/` | `main.py` (`/api/admin/*`), `ai_request_analysis.py`, `ai_audit_log_analysis.py` |

How the pieces decide what is allowed:

1. A message from a Patient first goes through fixed safety rules (emergencies, prescriptions,
   another person's information) that never reach the model.
2. The model only proposes an intent; `action_gateway.py` decides Allow, Block or Escalate from the
   user's role and permissions.
3. Records are only read for the signed-in Patient. A request for anyone else's information is a Block.
4. Each decision is written to `audit_logs` without message text.

## Repository layout

```text
database/    database_setup.sql, demo_seed.sql, optional_016_*.sql
backend/     app/ (FastAPI), guardrails/ (NeMo configs), tests/
frontend/    src/app, src/shared, src/modules/{admin,patient,technician}
```

## Set up the database

Use a new Supabase project (or any PostgreSQL 15+ with Supabase's `auth` schema).

1. In the SQL Editor, run [`database/database_setup.sql`](./database/database_setup.sql). It creates the
   tables, row-level security, grants, permission rows, triggers and functions in one transaction.
2. In **Authentication → Providers**, enable Email. Accounts are invited by an Admin in the app;
   there is no public sign-up. Allow `http://localhost:5173/set-password` under
   **Authentication → URL Configuration**.
3. Create your first Admin: add an Auth user in Supabase, then insert a matching row in
   `public.users` with role `Admin` and the Auth user's id in `auth_user_id`.
4. Optional demo schedule: edit the Physician email in [`database/demo_seed.sql`](./database/demo_seed.sql)
   and run it, so patients can book appointments.

By default a Patient can have one open correction request at a time. To allow several (useful for demos),
run [`database/optional_016_allow_multiple_corrections.sql`](./database/optional_016_allow_multiple_corrections.sql);
[`optional_016_rollback.sql`](./database/optional_016_rollback.sql) undoes it.

## Configure

```powershell
Copy-Item frontend\.env.example frontend\.env
Copy-Item backend\.env.example backend\.env
```

- `frontend/.env`: `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY` (public values only).
- `backend/.env`: `SUPABASE_URL`, `SUPABASE_KEY` (publishable key) and `SUPABASE_SERVICE_ROLE_KEY`
  (server-side only; never put it in the frontend). `FRONTEND_URL` defaults to `http://localhost:5173`.
- Feature flags in `backend/.env`: `HASF_CHAT_HISTORY_ENABLED`, `HASF_SERVICE_REQUESTS_ENABLED`, and
  `HASF_APPOINTMENTS_ENABLED` (needs service requests on too). All default to `false`.
- AI assistant: install [Ollama](https://ollama.com), run `ollama pull qwen2.5:3b`, and keep it running.
  `OLLAMA_URL` and `OLLAMA_MODEL` override the defaults. If Ollama is down the assistant reports it
  is unavailable instead of guessing.

## Run

```powershell
# backend
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --reload-dir app --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. API docs are at `http://localhost:8000/docs`, health at `/api/health`.

## Tests

```powershell
cd backend;  python -m pip install pytest; python -m pytest tests
cd frontend; npm test
```

The backend tests use a fake Supabase and need no database or network; two checks need `nemoguardrails`
installed. `backend/tests/pg/test_database_setup.py` builds an empty database from `database/database_setup.sql`
and checks RLS, the correction workflow, double booking and concurrency. It is skipped unless you point it at a
local throwaway PostgreSQL 15+ with the `psql` client (never Supabase):

```powershell
$env:HASF_PG_TEST_URI = "postgresql://postgres@127.0.0.1:5432/postgres"
python -m pytest backend/tests/pg
```

## Admin notes

- Admin sessions end after 10 minutes without activity; the backend also enforces this.
- Admin AI responses exclude patient ids, free-text details and request contents. No Admin endpoint
  returns patient medical records.
- The **Ask AI** panel (permission `monitor_ai_requests`) answers only from the latest 100 sanitized
  AI requests using the local Ollama model, and rejects questions about patient details.
- Admins invite users from **User management**; Supabase emails the invitation and the person sets
  their own password. Use a custom SMTP provider to invite addresses outside your Supabase team.
