-- 008_patient_chat_history.sql  (PROPOSED. NOT EXECUTED. Requires explicit approval.)
--
-- Saved Patient conversations. Plain text only: there are deliberately no columns
-- for authentication tokens, state/confirmation tokens, model prompts, or
-- correction details. Correction details never pass through the chat endpoint.
--
-- Access model: service-role only, called by FastAPI with p_user_id taken from the
-- verified token. RLS is enabled with no policies, so anon/authenticated roles are
-- denied. Functions are SECURITY INVOKER and never elevate.
--
-- BACKEND PERSISTENCE POLICY (enforced by the application; the database adds a
-- token-shape check as a last line of defense):
--   * Only turns whose reply passed the input and output rails are saved.
--   * Turns in the correction flow (request, offer, yes/no, cancel, review, confirm)
--     are saved only as fixed event text, never in the Patient's own words.
--   * Blocked or flagged messages (injection attempts, text that looks like personal
--     or medical details) are not saved; the API reports save_status = not_saved.
--   * If saving fails for any reason, the API reports not_saved. It never claims saved.
--
-- RETENTION LIMITATION: conversations expire 30 days after last activity and are
-- never returned or continuable after that (every read/write checks expires_at).
-- Physical deletion is NOT guaranteed on a schedule. Expired rows are removed only
-- (a) when the same Patient next SUCCESSFULLY saves a chat (a rejected save rolls the
-- purge back), (b) when the Patient deletes a chat,
-- or (c) when purge_expired_patient_chats() is run by a job that is not created here.
--
-- Assumes public.users(user_id integer) is a primary key (verify read-only first).
-- No existing table, role, grant or permission row is modified.

begin;

create table public.patient_chats (
  chat_id          uuid primary key,
  user_id          integer not null references public.users (user_id) on delete cascade,
  title            text not null check (char_length(title) between 1 and 60),
  message_count    integer not null default 0 check (message_count between 0 and 200),
  created_at       timestamptz not null default now(),
  last_activity_at timestamptz not null default now(),
  expires_at       timestamptz not null default now() + interval '30 days',
  unique (chat_id, user_id)
);
create index patient_chats_owner_idx   on public.patient_chats (user_id, last_activity_at desc);
create index patient_chats_expires_idx on public.patient_chats (expires_at);

create table public.patient_chat_messages (
  message_id   bigint generated always as identity primary key,
  chat_id      uuid not null,
  user_id      integer not null,
  turn_id      uuid not null,
  role         text not null check (role in ('user', 'assistant')),
  content      text not null check (char_length(content) between 1 and 2000),
  display_mode text check (display_mode in
               ('answer','refusal','notice','pointer','greeting','offer','details_step','cancelled','event')),
  created_at   timestamptz not null default now(),
  check (role <> 'user' or display_mode is null),
  -- Last line of defense: reject text shaped like a signed token (two long base64url
  -- segments joined by a dot, as in a JWT or an assistant state token) or a bearer header.
  check (content !~ '[A-Za-z0-9_-]{24,}[.][A-Za-z0-9_-]{24,}' and content !~* '\mbearer\s+\S'),
  -- A message always belongs to a chat owned by the same Patient.
  foreign key (chat_id, user_id) references public.patient_chats (chat_id, user_id) on delete cascade,
  -- A turn_id can be used once per Patient, across all of that Patient's chats.
  unique (user_id, turn_id, role)
);
create index patient_chat_messages_chat_idx on public.patient_chat_messages (chat_id, message_id);

comment on table public.patient_chats is
  'Patient-visible chat history. Not an audit record. Expires 30 days after last activity; physical deletion is lazy, not guaranteed.';
comment on table public.patient_chat_messages is
  'Plain chat text only. No tokens, prompts or correction details by design.';

alter table public.patient_chats         enable row level security;
alter table public.patient_chat_messages enable row level security;
revoke all on table public.patient_chats, public.patient_chat_messages from public, anon, authenticated;
grant select, insert, update, delete on table public.patient_chats, public.patient_chat_messages to service_role;

-- Read-only lookup the backend calls BEFORE invoking the model, so a retried turn
-- returns the stored reply instead of generating a new one.
--   {"found": false}                          -> new turn, proceed
--   {"found": true, "reply": ..., "mode": ..} -> already saved with the same chat and message
--   PT422                                     -> turn_id reused for another chat or message
-- The backend must pass the exact text it would persist (including any fixed placeholder).
create function public.find_patient_chat_turn(
  p_user_id   integer,
  p_chat_id   uuid,
  p_turn_id   uuid,
  p_user_text text)
returns jsonb
language plpgsql
stable
security invoker
set search_path = public, pg_temp
as $$
declare
  v_chat uuid;
  v_text text;
begin
  select m.chat_id, m.content into v_chat, v_text
  from public.patient_chat_messages m
  join public.patient_chats c on c.chat_id = m.chat_id and c.user_id = m.user_id
  where m.user_id = p_user_id and m.turn_id = p_turn_id and m.role = 'user'
    and c.expires_at > now();

  if not found then
    return jsonb_build_object('found', false);
  end if;

  if v_chat <> p_chat_id or v_text <> btrim(p_user_text) then
    raise exception using errcode = 'PT422', message = 'Turn id was already used for a different chat or message.';
  end if;

  return (select jsonb_build_object('found', true, 'chat_id', v_chat, 'reply', a.content, 'mode', a.display_mode)
          from public.patient_chat_messages a
          where a.user_id = p_user_id and a.turn_id = p_turn_id and a.role = 'assistant');
end;
$$;

-- Saves one turn (user message + assistant reply) atomically and idempotently.
--   p_is_new = true : p_chat_id is a client-generated id for a new chat (50-chat limit applies).
--                     Re-sending it after a lost response continues the same chat, never a second one.
--   p_is_new = false: p_chat_id must be an unexpired chat owned by p_user_id.
-- A repeated p_turn_id is accepted only for the same Patient, same chat and exact same
-- user message; the originally stored reply is returned. Any other reuse raises PT422.
create function public.save_patient_chat_turn(
  p_user_id    integer,
  p_chat_id    uuid,
  p_is_new     boolean,
  p_turn_id    uuid,
  p_user_text  text,
  p_reply_text text,
  p_reply_mode text)
returns jsonb
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_chat       uuid;
  v_count      integer;
  v_prior_chat uuid;
  v_prior_text text;
  v_user_text  text := btrim(p_user_text);
  v_reply_text text := btrim(p_reply_text);
begin
  if p_user_id is null or p_chat_id is null or p_is_new is null or p_turn_id is null
     or p_user_text is null or p_reply_text is null or p_reply_mode is null
     or char_length(v_user_text) not between 1 and 500
     or char_length(v_reply_text) not between 1 and 2000
     or p_reply_mode not in ('answer','refusal','notice','pointer','greeting','offer','details_step','cancelled','event') then
    raise exception using errcode = '22023', message = 'Invalid chat turn.';
  end if;

  if not exists (select 1 from public.users u where u.user_id = p_user_id) then
    raise exception using errcode = '42501', message = 'Not authorized.';
  end if;

  -- Serializes every chat write/delete for this Patient until the transaction ends,
  -- so limit checks, counters and duplicate detection cannot race.
  perform pg_advisory_xact_lock(hashtextextended('patient_chats:' || p_user_id::text, 0));

  -- Expired conversations cannot be continued; remove this Patient's expired chats.
  delete from public.patient_chats c where c.user_id = p_user_id and c.expires_at <= now();

  if exists (select 1 from public.patient_chats c where c.chat_id = p_chat_id and c.user_id <> p_user_id) then
    raise exception using errcode = 'PT404', message = 'Chat not found.';   -- another Patient's chat
  end if;

  -- Duplicate detection is per Patient across all chats.
  select m.chat_id, m.content into v_prior_chat, v_prior_text
  from public.patient_chat_messages m
  where m.user_id = p_user_id and m.turn_id = p_turn_id and m.role = 'user';

  if found then
    if v_prior_chat <> p_chat_id or v_prior_text <> v_user_text then
      raise exception using errcode = 'PT422', message = 'Turn id was already used for a different chat or message.';
    end if;
    return (select jsonb_build_object('chat_id', v_prior_chat, 'duplicate', true,
                                      'reply', a.content, 'mode', a.display_mode)
            from public.patient_chat_messages a
            where a.user_id = p_user_id and a.turn_id = p_turn_id and a.role = 'assistant');
  end if;

  select c.chat_id, c.message_count into v_chat, v_count
  from public.patient_chats c
  where c.chat_id = p_chat_id and c.user_id = p_user_id and c.expires_at > now();

  if v_chat is null then
    if not p_is_new then
      raise exception using errcode = 'PT404', message = 'Chat not found.';
    end if;
    if (select count(*) from public.patient_chats c where c.user_id = p_user_id) >= 50 then
      raise exception using errcode = 'PT429', message = 'Chat limit reached.';
    end if;
    insert into public.patient_chats (chat_id, user_id, title)
    values (p_chat_id, p_user_id, 'Chat of ' || to_char(now() at time zone 'utc', 'YYYY-MM-DD'));
    v_chat  := p_chat_id;
    v_count := 0;
  end if;

  if v_count + 2 > 200 then
    raise exception using errcode = 'PT409', message = 'Chat is full.';
  end if;

  -- One statement: both rows are inserted together or not at all.
  insert into public.patient_chat_messages (chat_id, user_id, turn_id, role, content, display_mode)
  values (v_chat, p_user_id, p_turn_id, 'user',      v_user_text,  null),
         (v_chat, p_user_id, p_turn_id, 'assistant', v_reply_text, p_reply_mode);

  update public.patient_chats
     set message_count    = v_count + 2,
         last_activity_at = now(),
         expires_at       = now() + interval '30 days'
   where chat_id = v_chat and user_id = p_user_id;

  return jsonb_build_object('chat_id', v_chat, 'duplicate', false);
end;
$$;

create function public.list_patient_chats(p_user_id integer)
returns jsonb
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'chat_id', c.chat_id, 'title', c.title,
           'last_activity_at', c.last_activity_at, 'message_count', c.message_count)
         order by c.last_activity_at desc), '[]'::jsonb)
  from public.patient_chats c
  where c.user_id = p_user_id and c.expires_at > now();
$$;

create function public.get_patient_chat_messages(p_user_id integer, p_chat_id uuid)
returns jsonb
language plpgsql
stable
security invoker
set search_path = public, pg_temp
as $$
begin
  if not exists (select 1 from public.patient_chats c
                 where c.chat_id = p_chat_id and c.user_id = p_user_id and c.expires_at > now()) then
    raise exception using errcode = 'PT404', message = 'Chat not found.';
  end if;
  return (select coalesce(jsonb_agg(jsonb_build_object(
                   'role', m.role, 'content', m.content,
                   'display_mode', m.display_mode, 'created_at', m.created_at)
                 order by m.message_id), '[]'::jsonb)
          from public.patient_chat_messages m
          where m.chat_id = p_chat_id and m.user_id = p_user_id);
end;
$$;

-- Deletes the Patient's own chat (expired or not). Cascades to its messages only;
-- audit_logs, ai_requests and approvals are never touched.
create function public.delete_patient_chat(p_user_id integer, p_chat_id uuid)
returns boolean
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_deleted integer;
begin
  perform pg_advisory_xact_lock(hashtextextended('patient_chats:' || p_user_id::text, 0));
  delete from public.patient_chats c where c.chat_id = p_chat_id and c.user_id = p_user_id;
  get diagnostics v_deleted = row_count;
  return v_deleted = 1;
end;
$$;

-- For a scheduled job, once one is approved. Nothing in this migration schedules it.
create function public.purge_expired_patient_chats()
returns integer
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_deleted integer;
begin
  delete from public.patient_chats where expires_at <= now();
  get diagnostics v_deleted = row_count;
  return v_deleted;
end;
$$;

revoke all on function
  public.find_patient_chat_turn(integer, uuid, uuid, text),
  public.save_patient_chat_turn(integer, uuid, boolean, uuid, text, text, text),
  public.list_patient_chats(integer),
  public.get_patient_chat_messages(integer, uuid),
  public.delete_patient_chat(integer, uuid),
  public.purge_expired_patient_chats()
from public, anon, authenticated;

grant execute on function
  public.find_patient_chat_turn(integer, uuid, uuid, text),
  public.save_patient_chat_turn(integer, uuid, boolean, uuid, text, text, text),
  public.list_patient_chats(integer),
  public.get_patient_chat_messages(integer, uuid),
  public.delete_patient_chat(integer, uuid),
  public.purge_expired_patient_chats()
to service_role;

commit;

-- Rollback (manual): drop the six functions, then patient_chat_messages, then patient_chats.
