#!/usr/bin/env bash
# Real-PostgreSQL validation of migration 008 on a DISPOSABLE local server.
#
# NOT RUN YET. Read README.md first. This script:
#   * refuses to run unless HASF_PG_TEST_CONFIRM=disposable and PGHOST is local;
#   * creates a brand-new database named hasf_disposable_<time>_<pid>, runs everything in it,
#     and drops it afterwards (KEEP_DB=1 keeps it for inspection);
#   * never reads backend/.env, never uses Supabase credentials.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATION="$HERE/../../migrations/008_patient_chat_history.sql"

die() { echo "REFUSING TO RUN: $*" >&2; exit 2; }

[ "${HASF_PG_TEST_CONFIRM:-}" = "disposable" ] || die "set HASF_PG_TEST_CONFIRM=disposable to confirm this is a throwaway server"
[ -n "${PGHOST:-}" ] || die "set PGHOST explicitly (localhost, 127.0.0.1, ::1 or a socket directory)"
case "$PGHOST" in
  localhost|127.0.0.1|::1|/*) ;;
  *) die "PGHOST='$PGHOST' is not a local address" ;;
esac
case "$PGHOST${PGDATABASE:-}${PGSERVICE:-}${DATABASE_URL:-}" in
  *supabase*|*pooler*) die "Supabase-looking connection settings are present" ;;
esac
[ -f "$MIGRATION" ] || die "migration file not found: $MIGRATION"
command -v psql >/dev/null || die "psql is not installed"

DB="hasf_disposable_$(date +%s)_$$"
ADMIN=(psql -X -P pager=off -v ON_ERROR_STOP=1 -q -d postgres)
PSQL=(psql -X -P pager=off -v ON_ERROR_STOP=1 -q -d "$DB")

# A tunnel or port-forward to a real server would still look "local", so also inspect the server:
# refuse Supabase-managed role names and any pre-existing non-template database.
SUPABASE_ROLES="$("${ADMIN[@]}" -t -A -c "select count(*) from pg_roles where rolname in ('supabase_admin','authenticator','supabase_auth_admin','supabase_storage_admin','pgbouncer')")"
[ "$SUPABASE_ROLES" = "0" ] || die "this server has Supabase-managed roles; it is not a throwaway server"
OTHER_DBS="$("${ADMIN[@]}" -t -A -c "select count(*) from pg_database where datname not in ('postgres','template0','template1') and datname not like 'hasf\_disposable\_%'")"
[ "$OTHER_DBS" = "0" ] || die "this server already hosts other databases; use a dedicated throwaway server"

[ "$("${ADMIN[@]}" -t -A -c "select rolsuper from pg_roles where rolname = current_user")" = "t" ] \
  || die "connect as a superuser of the throwaway server (the tests use SET ROLE)"
[ "$("${ADMIN[@]}" -t -A -c "show server_version_num")" -ge 130000 ] || die "PostgreSQL 13 or newer is required"

cleanup() {
  if [ "${KEEP_DB:-0}" = "1" ]; then
    echo "KEEP_DB=1: leaving database $DB in place"
  else
    "${ADMIN[@]}" -c "drop database if exists \"$DB\" with (force)" >/dev/null 2>&1 || true
  fi
  if [ -n "${WORK:-}" ]; then rm -rf "$WORK"; fi
}
trap cleanup EXIT
WORK="$(mktemp -d)"

echo "== creating $DB"
"${ADMIN[@]}" -c "create database \"$DB\""

echo "== harness (roles, Supabase-style default privileges, stub users, helpers)"
"${PSQL[@]}" -f "$HERE/00_harness.sql"

echo "== applying migration 008"
"${PSQL[@]}" -f "$MIGRATION"

echo "== re-applying must fail atomically and change nothing"
if "${PSQL[@]}" -f "$MIGRATION" >/dev/null 2>&1; then
  "${PSQL[@]}" -c "select pgtest.ok('re-running the migration fails', false)"
else
  "${PSQL[@]}" -c "select pgtest.ok('re-running the migration fails', true)"
fi

for f in 10_security.sql 20_behaviour.sql 30_rls_depth.sql; do
  echo "== $f"
  "${PSQL[@]}" -f "$HERE/$f"
done

# ------------------------------------------------------------------ concurrency
# Each job is its own psql session running as service_role. pgtest.try_save never raises; it
# prints ok:<duplicate> or err:<SQLSTATE>, which is what the assertions count.
echo "== concurrency"

# par DIR COUNT SQL_TEMPLATE  ('@I@' is replaced by the job number)
par() {
  local dir="$1" n="$2" tmpl="$3" i sql
  mkdir -p "$dir"
  for i in $(seq 1 "$n"); do
    sql="${tmpl//@I@/$i}"
    ( "${PSQL[@]}" -t -A -c "set role service_role; $sql" >"$dir/$i.out" 2>"$dir/$i.err" || echo "psql-failed" >>"$dir/$i.out" ) &
  done
  wait
}
outputs() { cat "$1"/*.out 2>/dev/null | sed 's/[[:space:]]//g' | sed '/^$/d'; }
tally()   { outputs "$1" | sort | uniq -c | awk '{printf "%s x%s  ", $2, $1}'; echo; }
count_of(){ outputs "$1" | grep -cx "$2" || true; }
sql1()    { "${PSQL[@]}" -t -A -c "$1"; }
assert_eq() {
  printf "select pgtest.ok(:'label', :'actual' = :'expected', 'actual=' || :'actual' || ' expected=' || :'expected');\n" \
    | "${PSQL[@]}" -q -t -A -v label="$1" -v actual="$2" -v expected="$3" >/dev/null
}

# S1: 20 identical requests (same turn id) at once -> exactly one real save.
par "$WORK/s1" 20 "select pgtest.try_save(1, pgtest.u(5000), true, pgtest.u(5001), 'same message', 'same reply')"
echo "S1: $(tally "$WORK/s1")"
assert_eq "S1 exactly one request saved" "$(count_of "$WORK/s1" 'ok:false')" 1
assert_eq "S1 the other 19 were duplicates" "$(count_of "$WORK/s1" 'ok:true')" 19
assert_eq "S1 two message rows" "$(sql1 "select count(*) from public.patient_chat_messages where chat_id = pgtest.u(5000)")" 2
assert_eq "S1 message_count is 2" "$(sql1 "select message_count from public.patient_chats where chat_id = pgtest.u(5000)")" 2

# S2: 20 different turns into one chat at once -> all saved, counters exact, no lost updates.
sql1 "set role service_role; select pgtest.try_save(2, pgtest.u(5100), true, pgtest.u(5101), 'first', 'ok')" >/dev/null
par "$WORK/s2" 20 "select pgtest.try_save(2, pgtest.u(5100), false, pgtest.u(5200 + @I@), 'turn @I@', 'reply @I@')"
echo "S2: $(tally "$WORK/s2")"
assert_eq "S2 all 20 saved" "$(count_of "$WORK/s2" 'ok:false')" 20
assert_eq "S2 42 message rows" "$(sql1 "select count(*) from public.patient_chat_messages where chat_id = pgtest.u(5100)")" 42
assert_eq "S2 message_count matches the rows" "$(sql1 "select message_count from public.patient_chats where chat_id = pgtest.u(5100)")" 42

# S3: 60 concurrent NEW chats for one Patient -> exactly 50 succeed.
par "$WORK/s3" 60 "select pgtest.try_save(4, pgtest.u(6000 + @I@), true, pgtest.u(6500 + @I@), 'q @I@', 'a @I@')"
echo "S3: $(tally "$WORK/s3")"
assert_eq "S3 exactly 50 chats created" "$(count_of "$WORK/s3" 'ok:false')" 50
assert_eq "S3 the other 10 hit the limit (PT429)" "$(count_of "$WORK/s3" 'err:PT429')" 10
assert_eq "S3 Patient has exactly 50 chats" "$(sql1 "select count(*) from public.patient_chats where user_id = 4")" 50

# S4: two Patients race to create the SAME chat id -> exactly one owner, never mixed data.
mkdir -p "$WORK/s4a" "$WORK/s4b"
( par "$WORK/s4a" 6 "select pgtest.try_save(1, pgtest.u(7000), true, pgtest.u(7100 + @I@), 'a @I@', 'ra @I@')" ) &
( par "$WORK/s4b" 6 "select pgtest.try_save(5, pgtest.u(7000), true, pgtest.u(7200 + @I@), 'b @I@', 'rb @I@')" ) &
wait
echo "S4a: $(tally "$WORK/s4a")"; echo "S4b: $(tally "$WORK/s4b")"
S4_OK=$(( $(count_of "$WORK/s4a" 'ok:false') + $(count_of "$WORK/s4b" 'ok:false') ))
S4_LOSER_ERR=$(( $(count_of "$WORK/s4a" 'err:PT404') + $(count_of "$WORK/s4a" 'err:23505') + $(count_of "$WORK/s4b" 'err:PT404') + $(count_of "$WORK/s4b" 'err:23505') ))
assert_eq "S4 the winner's 6 turns all saved" "$S4_OK" 6
assert_eq "S4 the loser's 6 attempts all failed cleanly (PT404 or 23505)" "$S4_LOSER_ERR" 6
assert_eq "S4 exactly one owner in the data" "$(sql1 "select count(distinct user_id) from public.patient_chat_messages where chat_id = pgtest.u(7000)")" 1
assert_eq "S4 12 message rows" "$(sql1 "select count(*) from public.patient_chat_messages where chat_id = pgtest.u(7000)")" 12

# S5: saves racing deletes on one chat -> no deadlock, no orphan rows, counters consistent.
sql1 "set role service_role; select pgtest.try_save(1, pgtest.u(8000), true, pgtest.u(8001), 'seed', 'ok')" >/dev/null
mkdir -p "$WORK/s5a" "$WORK/s5b"
( par "$WORK/s5a" 10 "select pgtest.try_save(1, pgtest.u(8000), false, pgtest.u(8100 + @I@), 'racing @I@', 'ok')" ) &
( par "$WORK/s5b" 5  "select pgtest.try_delete(1, pgtest.u(8000))" ) &
wait
echo "S5 saves:   $(tally "$WORK/s5a")"; echo "S5 deletes: $(tally "$WORK/s5b")"
S5_BAD=$( { outputs "$WORK/s5a"; outputs "$WORK/s5b"; } | grep -vcxE 'ok:false|ok:true|err:PT404' || true )
assert_eq "S5 only expected outcomes (no deadlock 40P01, no psql failures)" "$S5_BAD" 0
assert_eq "S5 no orphan messages" "$(sql1 "select count(*) from public.patient_chat_messages m where not exists (select 1 from public.patient_chats c where c.chat_id = m.chat_id)")" 0
assert_eq "S5 counter equals rows (or chat gone)" "$(sql1 "select coalesce((select message_count from public.patient_chats where chat_id = pgtest.u(8000)), (select count(*) from public.patient_chat_messages where chat_id = pgtest.u(8000)))::int = coalesce((select count(*) from public.patient_chat_messages where chat_id = pgtest.u(8000)), 0)")" t

# ------------------------------------------------------------------ summary
echo
echo "== results"
TOTAL="$(sql1 "select count(*) from pgtest.results")"
FAILS="$(sql1 "select count(*) from pgtest.results where not ok")"
echo "checks: $TOTAL, failed: $FAILS"
if [ "$FAILS" != "0" ]; then
  sql1 "select 'FAIL: ' || label || coalesce('  [' || detail || ']', '') from pgtest.results where not ok order by id"
  exit 1
fi
[ "$TOTAL" -ge 100 ] || { echo "Too few checks ran ($TOTAL); something was skipped." >&2; exit 1; }
echo "ALL CHECKS PASSED"
