-- 30_rls_depth.sql -- defense in depth: even if SELECT were wrongly granted to an API role,
-- RLS (enabled, no policies) must still hide every row. Runs after 20_behaviour.sql so there
-- is data. Disposable databases only.

\echo == 30_rls_depth ==

do $t$
declare n_all bigint; n_chats bigint; n_msgs bigint;
begin
  select count(*) into n_all from public.patient_chats;
  perform pgtest.ok('setup: rows exist to be hidden', n_all > 0, 'rows=' || n_all);

  grant select on public.patient_chats, public.patient_chat_messages to authenticated, anon;

  set local role authenticated;
  select count(*) into n_chats from public.patient_chats;
  select count(*) into n_msgs  from public.patient_chat_messages;
  reset role;
  perform pgtest.ok('authenticated sees 0 chats and 0 messages despite a SELECT grant', n_chats = 0 and n_msgs = 0,
    'chats=' || n_chats || ' messages=' || n_msgs);

  set local role anon;
  select count(*) into n_chats from public.patient_chats;
  reset role;
  perform pgtest.ok('anon sees 0 chats despite a SELECT grant', n_chats = 0, 'chats=' || n_chats);

  revoke select on public.patient_chats, public.patient_chat_messages from authenticated, anon;
  perform pgtest.ok('test grants were reverted', not has_table_privilege('authenticated', 'public.patient_chats', 'SELECT'));
end $t$;
