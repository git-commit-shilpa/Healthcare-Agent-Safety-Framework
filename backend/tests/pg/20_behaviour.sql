-- 20_behaviour.sql -- function behaviour: save, idempotency, ownership, validation,
-- constraints, limits, expiry and deletion. Disposable databases only.
-- Patients: 1 = A, 2 = B, 3 = C. Chat/turn ids come from pgtest.u(n).

\echo == 20_behaviour ==

-- ---------------------------------------------------------------- save and read back
do $t$
declare r jsonb;
begin
  r := pgtest.save(1, pgtest.u(1), true, pgtest.u(101), '  Hello there  ', 'Hi!', 'greeting');
  perform pgtest.ok('first save creates the chat', r->>'duplicate' = 'false' and r->>'chat_id' = pgtest.u(1)::text, r::text);
  perform pgtest.ok('message_count is 2', (select message_count from public.patient_chats where chat_id = pgtest.u(1)) = 2);
  perform pgtest.ok('user text is trimmed; assistant mode is kept',
    (select string_agg(role || ':' || content || ':' || coalesce(display_mode, '-'), '|' order by message_id)
       from public.patient_chat_messages where chat_id = pgtest.u(1)) = 'user:Hello there:-|assistant:Hi!:greeting');
  perform pgtest.ok('title is a dated label',
    (select title like 'Chat of 20__-__-__' from public.patient_chats where chat_id = pgtest.u(1)));
  perform pgtest.ok('expires_at is about 30 days out',
    (select expires_at between now() + interval '29 days 23 hours' and now() + interval '30 days 1 hour'
       from public.patient_chats where chat_id = pgtest.u(1)));

  r := pgtest.get(1, pgtest.u(1));
  perform pgtest.ok('get returns both messages in order',
    jsonb_array_length(r) = 2 and r->0->>'role' = 'user' and r->1->>'role' = 'assistant'
    and r->1->>'display_mode' = 'greeting' and r->0->'display_mode' = 'null'::jsonb, r::text);
  perform pgtest.ok('get exposes only role/content/display_mode/created_at',
    (select array_agg(k order by k) from jsonb_object_keys(r->0) k) = array['content', 'created_at', 'display_mode', 'role']);
end $t$;

-- ---------------------------------------------------------------- idempotency
do $t$
declare r jsonb;
begin
  r := pgtest.save(1, pgtest.u(1), true, pgtest.u(101), 'Hello there', 'A DIFFERENT REPLY', 'answer');
  perform pgtest.ok('repeat of a saved turn is a duplicate with the ORIGINAL reply',
    r->>'duplicate' = 'true' and r->>'reply' = 'Hi!' and r->>'mode' = 'greeting', r::text);
  perform pgtest.ok('a duplicate adds no rows',
    (select count(*) from public.patient_chat_messages where chat_id = pgtest.u(1)) = 2
    and (select message_count from public.patient_chats where chat_id = pgtest.u(1)) = 2);

  r := pgtest.save(1, pgtest.u(1), false, pgtest.u(101), '   Hello there ', 'x', 'answer');
  perform pgtest.ok('duplicate match ignores surrounding spaces and p_is_new', r->>'duplicate' = 'true', r::text);

  r := pgtest.find(1, pgtest.u(1), pgtest.u(101), ' Hello there ');
  perform pgtest.ok('find returns the stored reply for the same text',
    r->>'found' = 'true' and r->>'reply' = 'Hi!' and r->>'mode' = 'greeting', r::text);
  r := pgtest.find(1, pgtest.u(1), pgtest.u(999), 'never saved');
  perform pgtest.ok('find reports a new turn as not found', r = '{"found": false}'::jsonb, r::text);

  perform pgtest.expect_err('find: same turn, different text -> PT422', 'PT422',
    format($q$select pgtest.find(1, %L, %L, 'something else')$q$, pgtest.u(1), pgtest.u(101)));
  perform pgtest.expect_err('find: an empty probe text on a used turn -> PT422', 'PT422',
    format($q$select pgtest.find(1, %L, %L, '')$q$, pgtest.u(1), pgtest.u(101)));
  perform pgtest.expect_err('save: same turn, different text -> PT422', 'PT422',
    format($q$select pgtest.save(1, %L, false, %L, 'something else', 'r')$q$, pgtest.u(1), pgtest.u(101)));
  -- a second chat for A, then reuse turn 101 there
  perform pgtest.save(1, pgtest.u(2), true, pgtest.u(102), 'Second chat', 'Fine.');
  perform pgtest.expect_err('save: same turn id in another chat -> PT422', 'PT422',
    format($q$select pgtest.save(1, %L, false, %L, 'Hello there', 'r')$q$, pgtest.u(2), pgtest.u(101)));
  perform pgtest.expect_err('find: same turn id in another chat -> PT422', 'PT422',
    format($q$select pgtest.find(1, %L, %L, 'Hello there')$q$, pgtest.u(2), pgtest.u(101)));
  perform pgtest.ok('failed attempts left the data unchanged',
    (select count(*) from public.patient_chat_messages where user_id = 1) = 4);

  -- A lost response is retried with p_is_new = true and a NEW turn: the same chat continues.
  r := pgtest.save(1, pgtest.u(1), true, pgtest.u(103), 'Follow up', 'Sure.');
  perform pgtest.ok('re-sending p_is_new=true for an existing chat continues it',
    r->>'duplicate' = 'false' and (select count(*) from public.patient_chats where user_id = 1) = 2
    and (select message_count from public.patient_chats where chat_id = pgtest.u(1)) = 4, r::text);
end $t$;

-- ---------------------------------------------------------------- cross-patient ownership
do $t$
declare r jsonb;
begin
  r := pgtest.find(2, pgtest.u(1), pgtest.u(101), 'Hello there');
  perform pgtest.ok('find cannot see another Patient''s turn', r = '{"found": false}'::jsonb, r::text);

  perform pgtest.expect_err('B cannot save into A''s chat (p_is_new=false)', 'PT404',
    format($q$select pgtest.save(2, %L, false, %L, 'hi', 'yo')$q$, pgtest.u(1), pgtest.u(201)));
  perform pgtest.expect_err('B cannot claim A''s chat id (p_is_new=true)', 'PT404',
    format($q$select pgtest.save(2, %L, true, %L, 'hi', 'yo')$q$, pgtest.u(1), pgtest.u(202)));
  perform pgtest.expect_err('B cannot read A''s chat', 'PT404',
    format($q$select pgtest.get(2, %L)$q$, pgtest.u(1)));
  perform pgtest.ok('B cannot delete A''s chat',
    pgtest.del(2, pgtest.u(1)) = false and exists (select 1 from public.patient_chats where chat_id = pgtest.u(1) and user_id = 1));
  perform pgtest.ok('B''s list is empty; A''s lists only A''s chats',
    pgtest.list(2) = '[]'::jsonb and jsonb_array_length(pgtest.list(1)) = 2);
  -- now() is fixed inside one transaction, so make the ordering explicit before asserting it.
  update public.patient_chats set last_activity_at = now() - interval '1 hour' where chat_id = pgtest.u(2);
  perform pgtest.ok('A''s list is newest first', pgtest.list(1)->0->>'chat_id' = pgtest.u(1)::text);
  perform pgtest.ok('nothing leaked into the failed attempts', not exists (select 1 from public.patient_chat_messages where user_id = 2));

  -- Turn ids are scoped per Patient: B may use a turn id A already used.
  r := pgtest.save(2, pgtest.u(3), true, pgtest.u(101), 'B says hi', 'Hello B.');
  perform pgtest.ok('the same turn id is independent per Patient', r->>'duplicate' = 'false', r::text);
  perform pgtest.expect_err('unknown Patient -> 42501', '42501',
    format($q$select pgtest.save(999, %L, true, %L, 'hi', 'yo')$q$, pgtest.u(77), pgtest.u(777)));
end $t$;

-- ---------------------------------------------------------------- input validation (22023)
do $t$
begin
  perform pgtest.expect_err('blank user text -> 22023', '22023',
    format($q$select pgtest.save(1, %L, false, %L, '   ', 'r')$q$, pgtest.u(1), pgtest.u(301)));
  perform pgtest.expect_err('user text over 500 chars -> 22023', '22023',
    format($q$select pgtest.save(1, %L, false, %L, repeat('a', 501), 'r')$q$, pgtest.u(1), pgtest.u(302)));
  perform pgtest.expect_err('reply over 2000 chars -> 22023', '22023',
    format($q$select pgtest.save(1, %L, false, %L, 'q', repeat('a', 2001))$q$, pgtest.u(1), pgtest.u(303)));
  perform pgtest.expect_err('unknown reply mode -> 22023', '22023',
    format($q$select pgtest.save(1, %L, false, %L, 'q', 'r', 'bogus')$q$, pgtest.u(1), pgtest.u(304)));
  perform pgtest.expect_err('null reply -> 22023', '22023',
    format($q$select pgtest.save(1, %L, false, %L, 'q', null)$q$, pgtest.u(1), pgtest.u(305)));
  perform pgtest.expect_err('null turn id -> 22023', '22023',
    format($q$select pgtest.save(1, %L, false, null, 'q', 'r')$q$, pgtest.u(1)));
  perform pgtest.ok('boundary lengths are accepted (500 / 2000)',
    (pgtest.save(1, pgtest.u(1), false, pgtest.u(306), repeat('a', 500), repeat('b', 2000)))->>'duplicate' = 'false');
end $t$;

-- ---------------------------------------------------------------- database constraints
do $t$
declare
  tok text := repeat('a', 30) || '.' || repeat('b', 30);
begin
  perform pgtest.expect_err('token-shaped reply is rejected by the function (23514)', '23514',
    format($q$select pgtest.save(1, %L, false, %L, 'q', %L)$q$, pgtest.u(1), pgtest.u(401), 'here: ' || tok));
  perform pgtest.expect_err('token-shaped user text is rejected (23514)', '23514',
    format($q$select pgtest.save(1, %L, false, %L, %L, 'r')$q$, pgtest.u(1), pgtest.u(402), tok));
  perform pgtest.expect_err('"Bearer ..." text is rejected (23514)', '23514',
    format($q$select pgtest.save(1, %L, false, %L, 'Authorization: Bearer abc', 'r')$q$, pgtest.u(1), pgtest.u(403)));
  perform pgtest.ok('ordinary dotted text is still accepted',
    (pgtest.save(1, pgtest.u(1), false, pgtest.u(404), 'Visit example.com or see Mr. Smith.', 'See https://example.org/a.b'))->>'duplicate' = 'false');

  -- Direct DML (as the table owner) must hit the same CHECK / FK / UNIQUE rules.
  perform pgtest.expect_err('direct insert: token-shaped content -> 23514', '23514',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content) values (%L, 1, %L, 'assistant', %L)$q$, pgtest.u(1), pgtest.u(410), tok));
  perform pgtest.expect_err('direct insert: user row with display_mode -> 23514', '23514',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content, display_mode) values (%L, 1, %L, 'user', 'q', 'answer')$q$, pgtest.u(1), pgtest.u(411)));
  perform pgtest.expect_err('direct insert: bad role -> 23514', '23514',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content) values (%L, 1, %L, 'system', 'q')$q$, pgtest.u(1), pgtest.u(412)));
  perform pgtest.expect_err('direct insert: empty content -> 23514', '23514',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content) values (%L, 1, %L, 'assistant', '')$q$, pgtest.u(1), pgtest.u(413)));
  perform pgtest.expect_err('direct insert: content over 2000 -> 23514', '23514',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content) values (%L, 1, %L, 'assistant', repeat('a', 2001))$q$, pgtest.u(1), pgtest.u(414)));
  perform pgtest.expect_err('direct insert: bad display_mode -> 23514', '23514',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content, display_mode) values (%L, 1, %L, 'assistant', 'q', 'bogus')$q$, pgtest.u(1), pgtest.u(415)));
  perform pgtest.expect_err('direct insert: duplicate (user, turn, role) -> 23505', '23505',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content) values (%L, 1, %L, 'user', 'again')$q$, pgtest.u(1), pgtest.u(101)));
  perform pgtest.expect_err('direct insert: message for another Patient''s chat -> 23503', '23503',
    format($q$insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content) values (%L, 2, %L, 'assistant', 'q')$q$, pgtest.u(1), pgtest.u(416)));
  perform pgtest.expect_err('direct insert: chat for unknown user -> 23503', '23503',
    format($q$insert into public.patient_chats (chat_id, user_id, title) values (%L, 999, 't')$q$, pgtest.u(420)));
  perform pgtest.expect_err('direct insert: empty title -> 23514', '23514',
    format($q$insert into public.patient_chats (chat_id, user_id, title) values (%L, 1, '')$q$, pgtest.u(421)));
  perform pgtest.expect_err('direct insert: title over 60 -> 23514', '23514',
    format($q$insert into public.patient_chats (chat_id, user_id, title) values (%L, 1, repeat('t', 61))$q$, pgtest.u(422)));
  perform pgtest.expect_err('direct insert: message_count 201 -> 23514', '23514',
    format($q$insert into public.patient_chats (chat_id, user_id, title, message_count) values (%L, 1, 't', 201)$q$, pgtest.u(423)));
  perform pgtest.expect_err('direct insert: duplicate chat id -> 23505', '23505',
    format($q$insert into public.patient_chats (chat_id, user_id, title) values (%L, 2, 't')$q$, pgtest.u(1)));
end $t$;

-- ---------------------------------------------------------------- limits
do $t$
declare r jsonb; i integer;
begin
  -- 50 chats per Patient (Patient C)
  for i in 1..50 loop
    perform pgtest.save(3, pgtest.u(1000 + i), true, pgtest.u(2000 + i), 'q' || i, 'a' || i);
  end loop;
  perform pgtest.ok('Patient C has exactly 50 chats', (select count(*) from public.patient_chats where user_id = 3) = 50);
  perform pgtest.expect_err('51st chat -> PT429', 'PT429',
    format($q$select pgtest.save(3, %L, true, %L, 'q', 'a')$q$, pgtest.u(1051), pgtest.u(2051)));
  perform pgtest.ok('the failed 51st chat was not created', (select count(*) from public.patient_chats where user_id = 3) = 50);
  r := pgtest.save(3, pgtest.u(1001), false, pgtest.u(2101), 'more', 'ok');
  perform pgtest.ok('existing chats keep working at the limit', r->>'duplicate' = 'false', r::text);
  r := pgtest.save(3, pgtest.u(1002), true, pgtest.u(2102), 'more', 'ok');
  perform pgtest.ok('re-sending an existing chat id at the limit is not a new chat', r->>'duplicate' = 'false', r::text);
  perform pgtest.ok('the limit is per Patient', (pgtest.save(1, pgtest.u(3001), true, pgtest.u(3002), 'q', 'a'))->>'duplicate' = 'false');

  -- 200 messages per chat (Patient B, chat 3 already holds one turn)
  for i in 1..99 loop
    perform pgtest.save(2, pgtest.u(3), false, pgtest.u(4000 + i), 'q' || i, 'a' || i);
  end loop;
  perform pgtest.ok('chat reaches exactly 200 messages',
    (select message_count from public.patient_chats where chat_id = pgtest.u(3)) = 200
    and (select count(*) from public.patient_chat_messages where chat_id = pgtest.u(3)) = 200);
  perform pgtest.expect_err('turn 101 of a chat -> PT409', 'PT409',
    format($q$select pgtest.save(2, %L, false, %L, 'q', 'a')$q$, pgtest.u(3), pgtest.u(4100)));
  perform pgtest.ok('the rejected turn left the chat at 200',
    (select message_count from public.patient_chats where chat_id = pgtest.u(3)) = 200
    and (select count(*) from public.patient_chat_messages where chat_id = pgtest.u(3)) = 200);
  r := pgtest.save(2, pgtest.u(3), false, pgtest.u(4001), 'q1', 'a1');
  perform pgtest.ok('replaying an old turn in a full chat is still a duplicate, not PT409', r->>'duplicate' = 'true', r::text);
end $t$;

-- ---------------------------------------------------------------- expiry
do $t$
declare r jsonb; n integer;
begin
  update public.patient_chats set expires_at = now() - interval '1 day' where chat_id = pgtest.u(2);
  perform pgtest.ok('expired chats are hidden from the list',
    not (pgtest.list(1) @> jsonb_build_array(jsonb_build_object('chat_id', pgtest.u(2)::text))) and jsonb_array_length(pgtest.list(1)) = 2);
  perform pgtest.expect_err('expired chats cannot be read (PT404)', 'PT404',
    format($q$select pgtest.get(1, %L)$q$, pgtest.u(2)));
  r := pgtest.find(1, pgtest.u(2), pgtest.u(102), 'Second chat');
  perform pgtest.ok('find ignores expired chats', r = '{"found": false}'::jsonb, r::text);
  perform pgtest.expect_err('expired chats cannot be continued (PT404)', 'PT404',
    format($q$select pgtest.save(1, %L, false, %L, 'q', 'a')$q$, pgtest.u(2), pgtest.u(501)));
  perform pgtest.ok('a REJECTED save rolls back its purge (expired row still present)',
    exists (select 1 from public.patient_chats where chat_id = pgtest.u(2)));

  -- Scoped lazy purge: C saving purges only C's expired chats, never B's.
  update public.patient_chats set expires_at = now() - interval '1 day' where chat_id in (pgtest.u(3), pgtest.u(1006));
  perform pgtest.save(3, pgtest.u(1001), false, pgtest.u(2201), 'trigger purge', 'ok');
  perform pgtest.ok('a successful save purges that Patient''s expired chats',
    not exists (select 1 from public.patient_chats where chat_id = pgtest.u(1006)));
  perform pgtest.ok('...but not another Patient''s expired chats',
    exists (select 1 from public.patient_chats where chat_id = pgtest.u(3)));

  -- Starting over with the same id after expiry: the old chat and messages are gone first.
  r := pgtest.save(1, pgtest.u(2), true, pgtest.u(102), 'Second chat again', 'Fresh start.');
  perform pgtest.ok('an expired chat id can be reused; the old turn id is free again', r->>'duplicate' = 'false', r::text);
  perform pgtest.ok('only the new turn remains in the recreated chat',
    (select count(*) from public.patient_chat_messages where chat_id = pgtest.u(2)) = 2
    and (select message_count from public.patient_chats where chat_id = pgtest.u(2)) = 2);

  -- Activity extends the lifetime.
  update public.patient_chats set expires_at = now() + interval '1 day' where chat_id = pgtest.u(1);
  perform pgtest.save(1, pgtest.u(1), false, pgtest.u(504), 'keep alive', 'ok');
  perform pgtest.ok('saving renews expiry to about 30 days',
    (select expires_at > now() + interval '29 days' from public.patient_chats where chat_id = pgtest.u(1)));

  -- Global purge function.
  update public.patient_chats set expires_at = now() - interval '1 day' where chat_id in (pgtest.u(1002), pgtest.u(1003), pgtest.u(1004));
  n := (select count(*) from public.patient_chats where expires_at <= now());
  perform pgtest.ok('setup: there are expired chats to purge', n >= 4, 'n=' || n);
  perform pgtest.ok('purge returns the number of expired chats removed', pgtest.purge() = n, 'n=' || n);
  perform pgtest.ok('purge leaves no expired chats or orphan messages',
    not exists (select 1 from public.patient_chats where expires_at <= now())
    and not exists (select 1 from public.patient_chat_messages m where not exists
                      (select 1 from public.patient_chats c where c.chat_id = m.chat_id)));
  perform pgtest.ok('purge keeps live chats', exists (select 1 from public.patient_chats where chat_id = pgtest.u(1)));
  perform pgtest.ok('purge on nothing returns 0', pgtest.purge() = 0);
end $t$;

-- ---------------------------------------------------------------- deletion
do $t$
declare r jsonb;
begin
  perform pgtest.ok('owner can delete a chat', pgtest.del(1, pgtest.u(1)) = true);
  perform pgtest.ok('deleting cascades to messages',
    not exists (select 1 from public.patient_chat_messages where chat_id = pgtest.u(1)));
  perform pgtest.ok('deleting again returns false', pgtest.del(1, pgtest.u(1)) = false);
  perform pgtest.ok('deleting an unknown chat returns false', pgtest.del(1, pgtest.u(98765)) = false);
  perform pgtest.expect_err('a deleted chat cannot be read (PT404)', 'PT404',
    format($q$select pgtest.get(1, %L)$q$, pgtest.u(1)));
  r := pgtest.save(1, pgtest.u(1), true, pgtest.u(101), 'Hello there', 'Hi again!', 'greeting');
  perform pgtest.ok('a deleted chat id and its turn ids can be reused', r->>'duplicate' = 'false', r::text);

  -- An expired chat can still be deleted by its owner.
  update public.patient_chats set expires_at = now() - interval '1 day' where chat_id = pgtest.u(2);
  perform pgtest.ok('an expired chat can be deleted', pgtest.del(1, pgtest.u(2)) = true);

  -- Deleting the user cascades.
  delete from public.users where user_id = 3;
  perform pgtest.ok('deleting a user removes their chats and messages',
    not exists (select 1 from public.patient_chats where user_id = 3)
    and not exists (select 1 from public.patient_chat_messages where user_id = 3));
  -- (Patient B's only chat was removed by the global purge above, so use Patient A here.)
  perform pgtest.ok('other Patients are unaffected by that cascade',
    exists (select 1 from public.patient_chats where user_id = 1));
end $t$;
