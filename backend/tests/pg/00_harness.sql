-- 00_harness.sql -- DISPOSABLE DATABASES ONLY. Never run against Supabase.
--
-- Recreates just enough of a Supabase project for migration 008 to be tested:
--   * the three API roles (anon, authenticated, service_role) with Supabase-style
--     DEFAULT PRIVILEGES, so the test proves the migration's explicit REVOKEs win;
--   * a stub public.users(user_id) table (the real one is created outside migrations);
--   * a `pgtest` schema holding assertion helpers.
-- run_pg_tests.sh runs this, then the migration, then the 10_/20_/30_ scripts.

-- Cluster-level roles: use a dedicated throwaway container (see README.md).
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon')          then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role')  then create role service_role nologin bypassrls; end if;
end $$;

grant usage on schema public to anon, authenticated, service_role;
-- Supabase grants everything on new public objects to the API roles by default.
alter default privileges in schema public grant all on tables    to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;

create table public.users (
  user_id integer primary key,
  name    text,
  role    text
);
insert into public.users (user_id, name, role) values
  (1, 'Patient A', 'patient'), (2, 'Patient B', 'patient'), (3, 'Patient C', 'patient'),
  (4, 'Patient D', 'patient'), (5, 'Patient E', 'patient');

create schema pgtest;
grant usage on schema pgtest to anon, authenticated, service_role;

create table pgtest.results (
  id     bigserial primary key,
  label  text    not null,
  ok     boolean not null,
  detail text
);

-- Deterministic UUIDs: pgtest.u(7) = 00000000-0000-4000-8000-000000000007
create function pgtest.u(n integer) returns uuid
language sql immutable as
$$ select ('00000000-0000-4000-8000-' || lpad(n::text, 12, '0'))::uuid $$;

create function pgtest.ok(p_label text, p_ok boolean, p_detail text default null) returns void
language plpgsql as $$
begin
  insert into pgtest.results (label, ok, detail) values (p_label, coalesce(p_ok, false), p_detail);
  raise notice '% %', case when coalesce(p_ok, false) then 'PASS' else 'FAIL' end, p_label;
end $$;

-- Runs p_sql and checks it fails with SQLSTATE p_state. The inner block is a subtransaction,
-- so any SET LOCAL ROLE or partial writes made before the error are rolled back.
create function pgtest.expect_err(p_label text, p_state text, p_sql text) returns void
language plpgsql as $$
declare v_state text := 'none';
begin
  begin
    execute p_sql;
  exception when others then
    v_state := sqlstate;
  end;
  perform pgtest.ok(p_label, v_state = p_state, 'got ' || v_state || ', expected ' || p_state);
end $$;

-- Runs p_sql as p_role (superuser session -> SET ROLE) and discards the result.
create function pgtest.call_as(p_role text, p_sql text) returns void
language plpgsql as $$
begin
  execute format('set local role %I', p_role);
  execute p_sql;
  reset role;
end $$;

-- Thin service_role wrappers around the six functions, mirroring how FastAPI calls them.
create function pgtest.save(p_user integer, p_chat uuid, p_new boolean, p_turn uuid,
                            p_text text, p_reply text, p_mode text default 'answer') returns jsonb
language plpgsql as $$
declare v jsonb;
begin
  set local role service_role;
  v := public.save_patient_chat_turn(p_user, p_chat, p_new, p_turn, p_text, p_reply, p_mode);
  reset role;
  return v;
end $$;

create function pgtest.find(p_user integer, p_chat uuid, p_turn uuid, p_text text) returns jsonb
language plpgsql as $$
declare v jsonb;
begin
  set local role service_role;
  v := public.find_patient_chat_turn(p_user, p_chat, p_turn, p_text);
  reset role;
  return v;
end $$;

create function pgtest.list(p_user integer) returns jsonb
language plpgsql as $$
declare v jsonb;
begin
  set local role service_role;
  v := public.list_patient_chats(p_user);
  reset role;
  return v;
end $$;

create function pgtest.get(p_user integer, p_chat uuid) returns jsonb
language plpgsql as $$
declare v jsonb;
begin
  set local role service_role;
  v := public.get_patient_chat_messages(p_user, p_chat);
  reset role;
  return v;
end $$;

create function pgtest.del(p_user integer, p_chat uuid) returns boolean
language plpgsql as $$
declare v boolean;
begin
  set local role service_role;
  v := public.delete_patient_chat(p_user, p_chat);
  reset role;
  return v;
end $$;

create function pgtest.purge() returns integer
language plpgsql as $$
declare v integer;
begin
  set local role service_role;
  v := public.purge_expired_patient_chats();
  reset role;
  return v;
end $$;

-- Used by the concurrent sessions in run_pg_tests.sh: never raises, reports the outcome.
create function pgtest.try_save(p_user integer, p_chat uuid, p_new boolean, p_turn uuid,
                                p_text text, p_reply text) returns text
language plpgsql as $$
declare r jsonb;
begin
  r := public.save_patient_chat_turn(p_user, p_chat, p_new, p_turn, p_text, p_reply, 'answer');
  return 'ok:' || (r->>'duplicate');
exception when others then
  return 'err:' || sqlstate;
end $$;

create function pgtest.try_delete(p_user integer, p_chat uuid) returns text
language plpgsql as $$
begin
  return 'ok:' || public.delete_patient_chat(p_user, p_chat)::text;
exception when others then
  return 'err:' || sqlstate;
end $$;

grant execute on function pgtest.try_save(integer, uuid, boolean, uuid, text, text) to service_role;
grant execute on function pgtest.try_delete(integer, uuid) to service_role;
