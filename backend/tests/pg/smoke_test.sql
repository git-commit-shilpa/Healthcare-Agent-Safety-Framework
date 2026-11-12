-- Behaviour checks for database/database_setup.sql, run by test_database_setup.py on an empty throwaway database.
-- Fictional data only. Prints FAIL lines and one TOTAL line.
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned

create schema t;
create table t.results (label text, ok boolean, detail text);

-- Runs one statement as a role; expected_state = null means it must succeed, otherwise the SQLSTATE it must raise.
create function t.check(p_label text, p_stmt text, p_state text default null, p_role text default 'service_role')
returns void language plpgsql as $f$
declare v_state text; v_msg text;
begin
  begin
    execute 'set local role ' || p_role;
    execute p_stmt;
  exception when others then
    get stacked diagnostics v_msg = message_text;
    v_state := sqlstate;
  end;
  execute 'reset role';
  insert into t.results values (p_label,
    case when p_state is null then v_state is null else v_state = p_state end,
    coalesce('got ' || v_state || ': ' || left(v_msg, 80), 'no error'));
end $f$;

-- ---- fixtures (superuser) ----
insert into auth.users (id, email, email_confirmed_at) values
  ('00000000-0000-0000-0000-000000000001', 'patient.a@example.test', now()),
  ('00000000-0000-0000-0000-000000000002', 'patient.b@example.test', now()),
  ('00000000-0000-0000-0000-000000000003', 'tech@example.test', now()),
  ('00000000-0000-0000-0000-000000000004', 'physician@example.test', now()),
  ('00000000-0000-0000-0000-000000000005', 'admin@example.test', now());
insert into public.users (user_id, name, email, role, auth_user_id) overriding system value values
  (1, 'Patient A', 'patient.a@example.test', 'Patient',    '00000000-0000-0000-0000-000000000001'),
  (2, 'Patient B', 'patient.b@example.test', 'Patient',    '00000000-0000-0000-0000-000000000002'),
  (3, 'Tech T',    'tech@example.test',      'Technician', '00000000-0000-0000-0000-000000000003'),
  (4, 'Dr P',      'physician@example.test', 'Physician',  '00000000-0000-0000-0000-000000000004'),
  (5, 'Admin X',   'admin@example.test',     'Admin',      '00000000-0000-0000-0000-000000000005');
insert into public.patients (patient_id, user_id, age, medical_condition, treatment) overriding system value values
  (1, 1, 40, 'Fictional condition A', 'Fictional treatment A'),
  (2, 2, 50, 'Fictional condition B', 'Fictional treatment B');
insert into public.patient_assignments values (1, 3), (1, 4), (2, 3), (2, 4);

-- ---- 1. RLS and grants: browser roles see nothing; audit log is append-only ----
select t.check('anon cannot read users',            'select * from public.users',    '42501', 'anon');
select t.check('authenticated cannot read patients', 'select * from public.patients', '42501', 'authenticated');
select t.check('service role can read patients',     'select * from public.patients');
select t.check('audit_logs cannot be updated',       'update public.audit_logs set details = ''x''', '42501');
select t.check('audit_logs cannot be deleted',       'delete from public.audit_logs', '42501');
select t.check('authenticated cannot call the correction RPC', 'select public.submit_patient_treatment_correction(1, ''x'')', '42501', 'authenticated');

-- ---- 2. Patient -> Technician correction workflow ----
select t.check('patient A submits a correction (Technician first)', 'select public.submit_patient_treatment_correction(1, ''Fictional new treatment A'')');
select t.check('a second pending correction is refused by migration 015 rules', 'select public.submit_patient_treatment_correction(1, ''Another fictional change'')', 'PT409');
select t.check('correction needs a different value from the record', 'select public.submit_patient_treatment_correction(2, ''Fictional treatment B'')', '22023');
insert into t.results select 'correction has no approval yet', (select count(*) = 0 from public.approvals), 'approvals=' || (select count(*) from public.approvals);
insert into t.results select 'correction is an Escalate request with a Technician task',
  (select count(*) = 1 from public.ai_requests r join public.technician_tasks k on k.approval_request_id = r.request_id
    where r.guardrail_decision = 'Escalate' and k.category = 'record_correction' and k.assigned_to = 3 and k.status = 'Submitted'), '';

-- ---- 3. Physician-only clinical approval ----
select set_config('t.task', (select task_id::text from public.technician_tasks where category = 'record_correction' limit 1), false);
select t.check('a direct approval insert is refused unless it comes from the assigned Technician',
  'insert into public.approvals (request_id, reviewer_id, status) select request_id, 4, ''Pending'' from public.ai_requests limit 1', 'PT409');
select t.check('the assigned Technician starts the review',
  format('select public.start_technician_task_review(%s, 3)', current_setting('t.task')));
select t.check('another account cannot coordinate it',
  format('select public.coordinate_correction_task(%s, 4, ''request_physician_authorization'', ''n'', null)', current_setting('t.task')), 'PT403');
select t.check('Technician escalates the correction to the Physician',
  format('select public.coordinate_correction_task(%s, 3, ''request_physician_authorization'', ''ready'', null)', current_setting('t.task')));
insert into t.results select 'approval is Pending and addressed to the assigned Physician',
  (select count(*) = 1 from public.approvals where status = 'Pending' and reviewer_id = 4), '';
select t.check('once an approval exists the Technician cannot decline the correction',
  format('select public.coordinate_correction_task(%s, 3, ''decline_incomplete_information'', ''n'', ''msg'')', current_setting('t.task')), 'PT409');

-- ---- 4. Appointment scheduling and double-booking prevention ----
select t.check('Physician sets a weekly schedule', 'select public.upsert_schedule_rule(4, null, 1, 540, 720, 30, ''UTC'', null, null)');
select set_config('t.slot', to_char(date_trunc('week', now() + interval '21 days') + interval '10 hours', 'YYYY-MM-DD"T"HH24:MI:SS"+00"'), false);
select t.check('patient A requests a slot', format('select public.submit_appointment_request(1, 4, %L::timestamptz, ''Fictional check-up'', null)', current_setting('t.slot')));
select t.check('patient B can also request it (a request does not reserve the slot)', format('select public.submit_appointment_request(2, 4, %L::timestamptz, ''Fictional visit'', null)', current_setting('t.slot')));
select t.check('patient A cannot repeat the same request', format('select public.submit_appointment_request(1, 4, %L::timestamptz, ''Another'', null)', current_setting('t.slot')), 'PT409');
select set_config('t.apt_a', (select task_id::text from public.technician_tasks where category = 'appointment_scheduling' and requested_by = 1), false);
select set_config('t.apt_b', (select task_id::text from public.technician_tasks where category = 'appointment_scheduling' and requested_by = 2), false);
select t.check('Technician starts review of both requests',
  format('select public.start_technician_task_review(%s, 3), public.start_technician_task_review(%s, 3)', current_setting('t.apt_a'), current_setting('t.apt_b')));
select t.check('Technician confirms the first request',
  format('select public.record_technician_task_decision(%s, 3, ''ALLOW'', null, null, ''Your appointment is confirmed for that time.'', ''Allow'', ''low_risk_permitted'')', current_setting('t.apt_a')));
select t.check('confirming the second request for the same slot is refused (double booking)',
  format('select public.record_technician_task_decision(%s, 3, ''ALLOW'', null, null, ''Your appointment is confirmed for that time.'', ''Allow'', ''low_risk_permitted'')', current_setting('t.apt_b')), 'PT409');
insert into t.results select 'exactly one appointment holds the slot',
  (select count(*) = 1 from public.appointments where status = 'Confirmed'), '';

-- ---- 5. Privacy: chat history is private to its owner ----
select t.check('patient A saves a chat turn', 'select public.save_patient_chat_turn(1, ''11111111-1111-1111-1111-111111111111'', true, ''22222222-2222-2222-2222-222222222222'', ''fictional question'', ''fictional answer'', ''answer'')');
insert into t.results select 'patient B sees none of patient A''s chats',
  coalesce(jsonb_array_length(public.list_patient_chats(2)::jsonb), 0) = 0, '';
select t.check('patient B cannot read patient A''s chat', 'select public.get_patient_chat_messages(2, ''11111111-1111-1111-1111-111111111111'')', 'PT404');

-- ---- 6. Audit logging ----
insert into t.results select 'workflow steps were written to audit_logs',
  (select count(*) >= 2 from public.audit_logs), 'rows=' || (select count(*) from public.audit_logs);
insert into t.results select 'audit rows carry no free-text correction details',
  (select count(*) = 0 from public.audit_logs where details ilike '%Fictional new treatment A%'), '';

select 'FAIL|' || label || '|' || detail from t.results where not ok;
select 'TOTAL|' || count(*) || '|' || count(*) filter (where ok) from t.results;
