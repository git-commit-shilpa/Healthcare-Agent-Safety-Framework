# Real-PostgreSQL validation of migration 008 (chat history)

**Status: written, NOT yet executed.** `psql` was not available when this was authored, so the
SQL has not even been parsed. Expect to fix small syntax slips on the first run. Nothing here
touches Supabase.

The Python tests mock Supabase, so they cannot prove anything about the database. This kit runs
`migrations/008_patient_chat_history.sql` on a **throwaway local PostgreSQL** and checks the
security and behaviour claims directly.

## Safety rules (the runner enforces most of them)
- Local server only: `PGHOST` must be `localhost`, `127.0.0.1`, `::1` or a socket directory.
- You must set `HASF_PG_TEST_CONFIRM=disposable`.
- Everything runs in a new database `hasf_disposable_<time>_<pid>`, dropped at the end
  (`KEEP_DB=1` keeps it).
- The harness creates the cluster-level roles `anon`, `authenticated`, `service_role`. Use a
  dedicated throwaway server (Docker below), never a shared development server.
- The runner never reads `backend/.env` and uses no Supabase credentials.

## Running it (PostgreSQL 13+, 15 matches Supabase)
```bash
docker run --rm -d --name hasf-pgtest -e POSTGRES_PASSWORD=pgtest -p 54329:5432 postgres:15
export PGHOST=127.0.0.1 PGPORT=54329 PGUSER=postgres PGPASSWORD=pgtest
export HASF_PG_TEST_CONFIRM=disposable
bash backend/tests/pg/run_pg_tests.sh        # needs a psql client + Git Bash on Windows
docker stop hasf-pgtest
```
Exit code 0 and `ALL CHECKS PASSED` mean success. Any failure prints `FAIL: <label> [detail]`
and exits 1. The runner also fails if fewer than 100 checks ran, so a silently skipped file
cannot look like a pass.

## What each file covers
| File | Coverage |
|---|---|
| `00_harness.sql` | Roles; Supabase-style **default privileges** (so the migration's REVOKEs are tested against the grants Supabase would have added); stub `users`; helpers (`pgtest.ok`, `expect_err`, service_role wrappers). |
| `10_security.sql` | RLS enabled and zero policies; no table privileges for `anon`/`authenticated`/PUBLIC; `service_role` privileges; no EXECUTE for `anon`/`authenticated`/PUBLIC on all six functions; `SECURITY INVOKER`; pinned `search_path`; live attempts as `anon`/`authenticated` all fail with `42501`; no other `public` objects changed; keys, FKs, indexes. |
| `20_behaviour.sql` | Save and read-back, trimming; **duplicate turn** handling (`duplicate:true` with the ORIGINAL reply, `PT422` for other text or chat, `find` semantics incl. the empty-text probe); **cross-patient** isolation for find/save/get/delete/list; `22023` validation; **CHECK/FK/UNIQUE** constraints incl. token and `Bearer` rejection; **limits** (50 chats `PT429`, 200 messages `PT409`); **expiry** (hidden, unreadable, not continuable, scoped lazy purge, purge rolled back by a rejected save, id reuse, renewal, global purge); **deletion** (owner, cascade, expired, user-cascade). |
| `30_rls_depth.sql` | RLS still hides every row from `anon`/`authenticated` even if `SELECT` were wrongly granted. |
| `run_pg_tests.sh` | Re-applying the migration fails atomically; **concurrency** with separate sessions: S1 20 identical requests -> 1 save + 19 duplicates; S2 20 distinct turns in one chat -> exact counters; S3 60 new chats -> exactly 50 + 10 `PT429`; S4 two Patients race for one chat id -> single owner, no mixed data; S5 saves racing deletes -> no deadlock, no orphans, consistent counters. |

## Not covered (still needs separate verification)
- **PostgREST/HTTP layer:** that `PTnnn` becomes HTTP `nnn` and how errors are shaped. This kit
  tests SQL only. Verify on a disposable PostgREST container or in the staged Supabase test.
- **Real Supabase default ACLs:** the harness emulates them. Run the read-only catalog checks
  below on the real project (they only SELECT from catalogs) before any write test.
- **Authentication:** the database trusts `p_user_id`. Ownership of that value is enforced by the
  FastAPI backend and is covered by the Python tests, not here.
- **Logs:** whether Supabase/Postgres logs record RPC arguments on errors (design note M2).

## Read-only checks for the real project (not run; SELECT on catalogs only)
```sql
select to_regclass('public.patient_chats'), to_regclass('public.patient_chat_messages');
select relname, relrowsecurity from pg_class where relname like 'patient_chat%' and relkind = 'r';
select * from pg_policies where tablename like 'patient_chat%';            -- expect 0 rows
select grantee, table_name, privilege_type from information_schema.role_table_grants
  where table_name like 'patient_chat%';                                    -- expect service_role (and owner) only
select p.proname, p.prosecdef, p.proconfig,
       has_function_privilege('anon', p.oid, 'execute')          as anon_exec,
       has_function_privilege('authenticated', p.oid, 'execute') as auth_exec,
       has_function_privilege('service_role', p.oid, 'execute')  as svc_exec
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.proname in ('find_patient_chat_turn','save_patient_chat_turn',
       'list_patient_chats','get_patient_chat_messages','delete_patient_chat','purge_expired_patient_chats');
select conname, contype from pg_constraint where conrelid = 'public.users'::regclass;  -- users.user_id key
```
Expected: `anon_exec` and `auth_exec` false, `svc_exec` true, `prosecdef` false.

## Design note M2: raw text in `find_patient_chat_turn`
Today the backend sends the user's message to `find_patient_chat_turn` **before** the rails run,
so a replayed turn can return the stored reply without calling the model or consuming a
correction token. The text is only compared (never stored by that call), and messages that fail
the privacy filter are never sent. Residual exposure: text that passes the filter but is later
refused is still transmitted, and database error logs can include call arguments.

Recommended safe approach (no schema change, same duplicate detection and retry behaviour),
**not implemented yet**:
1. Probe with an empty string: `find(user, chat, turn, '')`. The function returns
   `{"found": false}` for a new turn and raises `PT422` for any used turn, because stored text is
   never empty. No patient text is sent for the common new-turn case. (`20_behaviour.sql` tests
   exactly this behaviour.)
2. Only when the probe says the turn exists, call `find` again with the candidate text(s) as now.
   That text was either already saved, or the attempt is a misuse that gets a `PT422`.

Trade-offs: one extra round trip on genuine replays only (rare); relies on `PT422` meaning "turn
exists", which is documented in the migration and covered by the PG tests; replays still send the
text once. The stronger long-term option is a keyed hash column (a new migration 009 that stores
an HMAC of the user text so `find` compares hashes). That removes text from the lookup entirely
but needs a schema change, a key to manage, and a rollout plan, so it is a later decision.
