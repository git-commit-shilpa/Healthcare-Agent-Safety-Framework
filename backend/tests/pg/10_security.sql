-- 10_security.sql -- structure, grants, RLS and execution privileges after migration 008.
-- Disposable databases only. Each pgtest.ok() records PASS/FAIL in pgtest.results.

\echo == 10_security ==

-- Both tables exist and have RLS enabled with no policies.
select pgtest.ok('patient_chats exists', to_regclass('public.patient_chats') is not null);
select pgtest.ok('patient_chat_messages exists', to_regclass('public.patient_chat_messages') is not null);
select pgtest.ok('RLS enabled: ' || c.relname, c.relrowsecurity)
from pg_class c where c.oid in ('public.patient_chats'::regclass, 'public.patient_chat_messages'::regclass);
select pgtest.ok('no RLS policies exist (deny by default)',
  (select count(*) from pg_policies where tablename in ('patient_chats', 'patient_chat_messages')) = 0);

-- Table privileges: anon/authenticated/PUBLIC have nothing, even though default privileges
-- (installed by 00_harness.sql, as on Supabase) granted them everything before the migration.
select pgtest.ok(r || ' has no ' || p || ' on ' || t, not has_table_privilege(r, 'public.' || t, p))
from unnest(array['anon', 'authenticated']) r,
     unnest(array['patient_chats', 'patient_chat_messages']) t,
     unnest(array['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) p;
select pgtest.ok('PUBLIC has no privileges on ' || c.relname,
  c.relacl is not null and not exists (select 1 from aclexplode(c.relacl) a where a.grantee = 0))
from pg_class c where c.oid in ('public.patient_chats'::regclass, 'public.patient_chat_messages'::regclass);
select pgtest.ok('service_role has ' || p || ' on ' || t, has_table_privilege('service_role', 'public.' || t, p))
from unnest(array['patient_chats', 'patient_chat_messages']) t,
     unnest(array['SELECT', 'INSERT', 'UPDATE', 'DELETE']) p;

-- Function privileges and definer/search_path hygiene.
create temp table fn_sigs (sig text);
insert into fn_sigs values
  ('public.find_patient_chat_turn(integer,uuid,uuid,text)'),
  ('public.save_patient_chat_turn(integer,uuid,boolean,uuid,text,text,text)'),
  ('public.list_patient_chats(integer)'),
  ('public.get_patient_chat_messages(integer,uuid)'),
  ('public.delete_patient_chat(integer,uuid)'),
  ('public.purge_expired_patient_chats()');

select pgtest.ok(r || ' cannot execute ' || sig, not has_function_privilege(r, sig, 'EXECUTE'))
from fn_sigs, unnest(array['anon', 'authenticated']) r;
select pgtest.ok('PUBLIC cannot execute ' || sig,
  (select p.proacl is not null and not exists (select 1 from aclexplode(p.proacl) a where a.grantee = 0)
     from pg_proc p where p.oid = sig::regprocedure))
from fn_sigs;
select pgtest.ok('service_role can execute ' || sig, has_function_privilege('service_role', sig, 'EXECUTE'))
from fn_sigs;
select pgtest.ok(sig || ' is SECURITY INVOKER', not (select p.prosecdef from pg_proc p where p.oid = sig::regprocedure))
from fn_sigs;
select pgtest.ok(sig || ' pins search_path',
  exists (select 1 from pg_proc p, unnest(p.proconfig) cfg
          where p.oid = sig::regprocedure and cfg like 'search_path=%pg_temp%'))
from fn_sigs;

-- Live attempts as the API roles: every one must be refused (SQLSTATE 42501).
select pgtest.expect_err('anon cannot read patient_chats', '42501',
  $q$select pgtest.call_as('anon', $x$select count(*) from public.patient_chats$x$)$q$);
select pgtest.expect_err('authenticated cannot read patient_chat_messages', '42501',
  $q$select pgtest.call_as('authenticated', $x$select count(*) from public.patient_chat_messages$x$)$q$);
select pgtest.expect_err('anon cannot insert patient_chats', '42501',
  $q$select pgtest.call_as('anon', $x$insert into public.patient_chats (chat_id, user_id, title) values (gen_random_uuid(), 1, 'x')$x$)$q$);
select pgtest.expect_err('authenticated cannot delete patient_chats', '42501',
  $q$select pgtest.call_as('authenticated', $x$delete from public.patient_chats$x$)$q$);
select pgtest.expect_err('anon cannot call list_patient_chats', '42501',
  $q$select pgtest.call_as('anon', $x$select public.list_patient_chats(1)$x$)$q$);
select pgtest.expect_err('authenticated cannot call save_patient_chat_turn', '42501',
  $q$select pgtest.call_as('authenticated', format($x$select public.save_patient_chat_turn(1, %L, true, %L, 'q', 'a', 'answer')$x$, pgtest.u(9001), pgtest.u(9002)))$q$);
select pgtest.expect_err('authenticated cannot call purge_expired_patient_chats', '42501',
  $q$select pgtest.call_as('authenticated', $x$select public.purge_expired_patient_chats()$x$)$q$);
select pgtest.expect_err('anon cannot call delete_patient_chat', '42501',
  $q$select pgtest.call_as('anon', format($x$select public.delete_patient_chat(1, %L)$x$, pgtest.u(9001)))$q$);
select pgtest.expect_err('anon cannot call get_patient_chat_messages', '42501',
  $q$select pgtest.call_as('anon', format($x$select public.get_patient_chat_messages(1, %L)$x$, pgtest.u(9001)))$q$);
select pgtest.expect_err('anon cannot call find_patient_chat_turn', '42501',
  $q$select pgtest.call_as('anon', format($x$select public.find_patient_chat_turn(1, %L, %L, 'q')$x$, pgtest.u(9001), pgtest.u(9002)))$q$);
select pgtest.ok('service_role can read through RLS (bypassrls)',
  pgtest.list(1) = '[]'::jsonb);

-- The migration must not have touched existing HASF objects.
select pgtest.ok('migration left public.users untouched (columns)',
  (select string_agg(column_name, ',' order by ordinal_position) from information_schema.columns
    where table_schema = 'public' and table_name = 'users') = 'user_id,name,role');
select pgtest.ok('migration added exactly 2 tables and 6 functions to public',
  (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r' and c.relname <> 'users') = 2
  and (select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public') = 6);

-- Table constraints and indexes the plan relies on.
select pgtest.ok('unique (user_id, turn_id, role) exists',
  exists (select 1 from pg_constraint where conrelid = 'public.patient_chat_messages'::regclass
          and contype = 'u' and pg_get_constraintdef(oid) = 'UNIQUE (user_id, turn_id, role)'));
select pgtest.ok('composite FK (chat_id, user_id) cascades',
  exists (select 1 from pg_constraint where conrelid = 'public.patient_chat_messages'::regclass
          and contype = 'f' and pg_get_constraintdef(oid) like 'FOREIGN KEY (chat_id, user_id) REFERENCES patient_chats(chat_id, user_id) ON DELETE CASCADE'));
select pgtest.ok('patient_chats.user_id references users ON DELETE CASCADE',
  exists (select 1 from pg_constraint where conrelid = 'public.patient_chats'::regclass
          and contype = 'f' and pg_get_constraintdef(oid) like 'FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE'));
select pgtest.ok('index ' || i, to_regclass('public.' || i) is not null)
from unnest(array['patient_chats_owner_idx', 'patient_chats_expires_idx', 'patient_chat_messages_chat_idx']) i;
