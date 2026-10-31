begin;

do $migration$
declare
  v_sequence regclass;
  v_max_request_id bigint;
  v_sequence_last_value bigint;
begin
  v_sequence := pg_catalog.pg_get_serial_sequence(
    'public.ai_requests',
    'request_id'
  )::regclass;

  if v_sequence is null then
    raise exception using
      errcode = '55000',
      message = 'public.ai_requests.request_id is not backed by an identity sequence; no change made.';
  end if;

  -- SHARE ROW EXCLUSIVE conflicts with the ROW EXCLUSIVE lock taken by
  -- INSERT, so no new request ids can be generated until this transaction
  -- commits. SELECT queries continue to work.
  lock table public.ai_requests in share row exclusive mode;

  select coalesce(max(request_id), 0)
  into v_max_request_id
  from public.ai_requests;

  -- NULL when the sequence has never been used.
  v_sequence_last_value := pg_catalog.pg_sequence_last_value(v_sequence);

  if v_max_request_id > coalesce(v_sequence_last_value, 0) then
    -- is_called = true: the next nextval() returns v_max_request_id + 1.
    perform pg_catalog.setval(v_sequence, v_max_request_id, true);
    raise notice 'ai_requests sequence advanced from % to %; next request_id will be %.',
      coalesce(v_sequence_last_value::text, 'unused'),
      v_max_request_id,
      v_max_request_id + 1;
  else
    raise notice 'ai_requests sequence (%) is already at or beyond the maximum request_id (%); no change made.',
      v_sequence_last_value,
      v_max_request_id;
  end if;
end;
$migration$;

commit;
