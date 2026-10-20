# Healthcare Agent Safety Framework

This project has a shared React/Vite frontend and a FastAPI backend. The
authentication foundation supports Admin, Patient, Physician, and Technician
accounts. Role pages are placeholders; they do not expose patient records or
implement the role-specific modules.

## Project layout

```text
frontend/
  src/
    app/                 Shared login and role-protected routes
    shared/              Supabase client and reusable UI/styles
    modules/admin/       Admin screens for Phase 3
    modules/<role>/      Teammates' independent role modules
backend/
  app/                   FastAPI application and authorization
  migrations/            Additive Auth-link and account-link SQL
```

The existing `public.users` and `public.permissions` tables are used as-is
except for the additive `auth_user_id` column in the migration below. No
existing rows or roles are deleted or rewritten.

## Prerequisites

- Node.js LTS with npm
- Python 3.10 or newer
- Your existing Supabase project

```powershell
node --version
npm --version
python --version
```

## Configure Supabase

1. In the Supabase Dashboard, open **Project Settings → API**. Copy the
   **Project URL** and the **Publishable key** (or legacy `anon` key).
2. Create the frontend environment file from the project root:

   ```powershell
   if (-not (Test-Path frontend\.env)) { Copy-Item frontend\.env.example frontend\.env }
   ```

   Set `VITE_SUPABASE_URL` and `VITE_SUPABASE_PUBLISHABLE_KEY` in
   `frontend/.env` (or add them to the file you already created in Phase 1).
   The Vite prefix makes these two public values available to the browser;
   never put a service-role or secret key in this file.
3. Create the backend environment file:

   ```powershell
   if (-not (Test-Path backend\.env)) { Copy-Item backend\.env.example backend\.env }
   ```

   Set `SUPABASE_URL`, `SUPABASE_KEY` to the same Project URL and
   Publishable/anon key, and set `SUPABASE_SERVICE_ROLE_KEY` to the project's server-side `service_role`
   JWT key (shown under the legacy API keys in Supabase). This privileged key
   is only for FastAPI's server-side role/permission lookups, account invites,
   and restricted Admin reads. `FRONTEND_URL` defaults to
   `http://localhost:5173` for local development; set it to your deployed
   frontend origin in production, for example
   `FRONTEND_URL=https://your-frontend.example.com`. Keep the service-role key
   only in `backend/.env`, never in frontend
   variables, source control, screenshots, or client requests. `.gitignore`
   excludes real `.env` files.
4. In Supabase **Authentication → Providers**, enable Email authentication.
   Accounts can be invited by an authorized Admin from this app; the project
   does not provide public sign-up or let invitees choose their own role. In
   **Authentication → URL Configuration**, allow the redirect URL
   `http://localhost:5173/set-password` during local development. Configure
   SMTP/email templates before relying on invitations in production.

## Preserve and link existing users

Run the SQL files in order in the Supabase SQL Editor:

1. Run [`backend/migrations/001_add_auth_user_link.sql`](./backend/migrations/001_add_auth_user_link.sql).
   It adds a nullable UUID reference to `auth.users`, sets account deletion to
   clear only that reference, and adds a unique index. Existing integer
   `user_id`, email, role, and all other rows remain unchanged.
2. Create/invite the intended Auth users and verify their email addresses.
3. Review the email matching process with your project administrator, then run
   [`backend/migrations/002_link_verified_auth_accounts.sql`](./backend/migrations/002_link_verified_auth_accounts.sql).
   It links only case-insensitive exact email matches that are unique in both
   tables and verified in Auth; it only updates rows whose Auth link is empty.
   Ambiguous and unmatched records are left untouched. For those records,
   resolve the identity manually before assigning an Auth user ID; do not
   link based only on a display name.
4. To enable the Admin AI-monitoring view, review and run
   [`backend/migrations/003_add_admin_ai_monitor_permission.sql`](./backend/migrations/003_add_admin_ai_monitor_permission.sql).
   It inserts the `Admin` / `monitor_ai_requests` / `true` permission only if
   that exact permission is not already present. It does not update or delete
   any existing permission or application record.
5. To enable Admin account deactivation, review and run
   [`backend/migrations/004_add_user_active_status.sql`](./backend/migrations/004_add_user_active_status.sql).
   It adds `public.users.is_active` with a default of `true`; existing user
   rows and their history are preserved.

If accounts are already present, back up the database and inspect each
migration before running it. These SQL files do not drop tables or delete
application data; migration 003 adds the requested Admin permission, and
migration 004 adds an active-status flag that defaults existing accounts to
active.

## Install and run

Open two VS Code terminals at the project root.

**Terminal 1 — backend:**

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env with the Supabase values before starting the API.
python -m uvicorn app.main:app --reload --reload-dir app --port 8000
```

If PowerShell blocks activation, run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal,
then activate again. This changes the policy only for that terminal session.
Watching only `app` prevents Uvicorn from restarting when packages change in
the `.venv` directory.

**Terminal 2 — frontend:**

```powershell
cd frontend
npm install
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env with the Supabase URL and Publishable/anon key.
npm run dev
```

Open the local URL Vite prints (normally `http://localhost:5173`). Login uses
Supabase Auth. After login, the frontend asks FastAPI to verify the session and
loads the role from the linked database row; it redirects to the matching role
dashboard. FastAPI validates each access token through Supabase Auth and reads
the current role and allowed actions from the existing `users` and
`permissions` tables. Role dashboard routes and permission-gated Admin
endpoints are checked on the backend as well as the frontend.
Authentication sessions are stored in the current tab's session storage, so
switching tabs or windows and reloading the page preserves the session. Closing
the tab or signing out ends it; sessions are not shared between tabs. Session
startup uses one bootstrap request after a page load; for Admin, that same
response includes the permission-filtered overview to avoid a second
authentication round trip before the dashboard can display. While the app
remains open, auth events for the same signed-in user update the token without
reloading dashboard state or making another bootstrap request.

On the Admin page, the four data sections load through one permission-checked
overview request; FastAPI fetches the sections concurrently and skips any
section the current Admin is not allowed to see. React development mode can
replay effects; identical overlapping GETs are coalesced in the frontend, and
the initial Supabase session is read from one auth-state event. Use
**Refresh data** only when you want to reload those sections.
The Admin dashboard does not make the separate placeholder role-dashboard
request. FastAPI also reuses Supabase HTTP connections and logs the duration
and status of each Supabase request so slow network or database calls can be
identified in the backend terminal.

Useful checks:

- `http://localhost:8000/api/health` — database connectivity
- `http://localhost:8000/api/auth/me` — requires `Authorization: Bearer YOUR_ACCESS_TOKEN`
- `http://localhost:8000/docs` — API documentation

The Admin dashboard reads users and the existing permissions matrix using
`manage_users`, AI-request activity using `monitor_ai_requests`, and audit-log
metadata using `view_audit_logs`. The matrix is read-only; no endpoint in this
phase changes roles or permissions. An Admin with `manage_users` can deactivate
another account; the API marks it inactive, blocks further application access,
and bans its linked Supabase Auth account. The user row, patient links, and
historical requests and audit records are not deleted. Admins cannot deactivate
their own account. AI responses exclude `patient_id`,
`requested_by`, `request_details`, and `decision_reason`. Audit responses
exclude `user_id` and free-text `details`. Each list is bounded and paginated
by the API; the dashboard displays the first page only. No patient medical
record endpoint is provided to Admin. Keep future Admin features behind
backend permission checks; frontend route guards are not authorization.

## Admin AI request analysis

The **Ask AI** panel sends questions to FastAPI, which requires the
`monitor_ai_requests` permission. FastAPI retrieves at most the latest 100 AI
requests. The regular request list contains only request ID, action, guardrail
decision, and timestamp. For analysis only, FastAPI also reads `decision_reason`
within the permission-checked endpoint. It exact-matches the value against an
approved reason allowlist and sends only curated wording for approved entries;
raw reason text, patient IDs, and request contents are never sent to the model.
Unknown or free-form reasons are discarded. Explanations
label approved reasons as recorded facts and potential security implications as
interpretation. Request summaries and explanations use the configured local
Ollama model. Requests for clinical details or patient-to-request associations
are rejected deterministically before the model call. Analyses cover only the
latest 100 requests (or fewer).

Install Ollama for your operating system, then pull the recommended local
model in a terminal:

```powershell
ollama pull qwen2.5:3b
ollama list
```

Keep Ollama running locally. The backend defaults to
`OLLAMA_URL=http://127.0.0.1:11434` and `OLLAMA_MODEL=qwen2.5:3b`; override
these in `backend/.env` if your local Ollama uses another model or port. The
analysis endpoint has a 120-second model timeout to accommodate the first
model load. If Ollama is stopped or the model is missing, the endpoint returns
an explicit error and does not fabricate an answer.
For local development, set `APP_ENV=development` in `backend/.env` to log
whether each Ask AI question used Ollama; these logs do not include questions,
request IDs, record contents, or credentials. Leave this unset outside
development.

To test, sign in as an Admin and ask, for example:

- `Summarize all blocked requests`
- `Which requests were escalated?`
- `Why was request 2 blocked?`
- `What are the most important AI request decisions?`

Answers should be grounded in the latest 100 sanitized rows. A rationale
question should state an approved reason as a recorded fact when available;
otherwise it should explain that the exact recorded reason is unavailable.
Non-Admin users or
users without `monitor_ai_requests` are rejected by FastAPI.

## Admin invitations

An Admin with `manage_users` can invite a person from the **User management**
panel by entering a name, email, and assigned role. FastAPI sends the invitation
through Supabase Auth, then creates the matching `public.users` row already
linked to that Auth user. Passwords are never set or received by the app:
Supabase sends the invitation email, and the recipient sets their password on
the `/set-password` page. The invite action is appended to `audit_logs`.
Duplicate application emails are rejected. If Supabase sends the invitation
but the database insert or audit write fails, the API reports that partial
failure explicitly; do not retry blindly—check Auth users, `public.users`, and
`audit_logs` first.

In Supabase Auth URL Configuration, allow the exact callback URL configured by
`FRONTEND_URL` plus `/set-password`. Supabase's default email sender is
development-only and may only deliver to project team addresses; configure a
custom SMTP provider and invitation email template to send to other users.
