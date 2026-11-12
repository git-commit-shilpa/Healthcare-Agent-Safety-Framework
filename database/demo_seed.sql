-- Demo data: a weekly schedule for one Physician so patients can request appointments.
-- Change the email to your Physician account, then run this once after database_setup.sql.
begin;

do $seed$
declare
  v_physician_email constant text := 'physician@example.test';
  v_timezone        constant text := 'America/New_York';
  -- weekday (0 = Sunday), start minute, end minute (after local midnight), slot length in minutes
  v_windows constant integer[][] := array[
    [1,  9 * 60, 12 * 60, 30],   -- Monday 09:00-12:00
    [3, 13 * 60, 16 * 60, 30]    -- Wednesday 13:00-16:00
  ];
  v_physician_id integer;
  i integer;
begin
  select user_id into v_physician_id
  from public.users
  where email = v_physician_email and role = 'Physician' and is_active;

  if v_physician_id is null then
    raise exception 'No active Physician with email %. Edit v_physician_email first.', v_physician_email;
  end if;

  for i in 1 .. array_length(v_windows, 1) loop
    perform public.upsert_schedule_rule(
      v_physician_id, null, v_windows[i][1], v_windows[i][2], v_windows[i][3], v_windows[i][4],
      v_timezone, null, null
    );
  end loop;
end;
$seed$;

commit;
