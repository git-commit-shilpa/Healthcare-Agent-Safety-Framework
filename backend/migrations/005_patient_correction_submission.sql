begin;

-- p_requested_by must come from a verified FastAPI authentication context;
-- never populate it from browser-supplied identity fields.
create or replace function public.submit_patient_treatment_correction(
  p_requested_by integer,
  p_proposed_treatment text
)
returns jsonb
language plpgsql
security invoker
set search_path = pg_catalog
as $function$
declare
  v_patient_id public.patients.patient_id%type;
  v_current_treatment public.patients.treatment%type;
  v_patient_count bigint;
  v_request_id public.ai_requests.request_id%type;
  v_reviewer_id public.users.user_id%type;
  v_candidate_reviewer_id public.users.user_id%type;
  v_normalized_current_treatment text;
  v_reviewer_count integer := 0;
  v_normalized_treatment text;
begin
  if p_requested_by is null
     or p_proposed_treatment is null
     or length(btrim(p_proposed_treatment)) = 0
     or length(btrim(p_proposed_treatment)) > 2000 then
    raise exception using
      errcode = '22023',
      message = 'Invalid treatment correction proposal.';
  end if;

  v_normalized_treatment := btrim(p_proposed_treatment);

  if p_requested_by <= 0 then
    raise exception using
      errcode = '22023',
      message = 'Invalid treatment correction requester.';
  end if;

  -- Lock the requester account while validating its role and active status.
  perform 1
  from public.users as requester
  where requester.user_id = p_requested_by
  for share;

  if not exists (
    select 1
    from public.users as requester
    where requester.user_id = p_requested_by
      and requester.role = 'Patient'
      and requester.is_active is true
  ) then
    raise exception using
      errcode = '42501',
      message = 'The requester is not an active Patient account.';
  end if;

  -- Lock every matching permission row: concurrent revocation or edits of an
  -- existing grant wait until this transaction completes. A concurrent grant
  -- does not invalidate a grant already established by this check.
  perform 1
  from public.permissions as permission
  where permission.role = 'Patient'
    and permission.action = 'submit_record_correction'
  for share;

  if not exists (
    select 1
    from public.permissions as permission
    where permission.role = 'Patient'
      and permission.action = 'submit_record_correction'
      and permission.allowed is true
  ) then
    raise exception using
      errcode = '42501',
      message = 'The Patient account is not permitted to submit corrections.';
  end if;

  -- Lock linked Patient rows before inspecting them. This stabilizes the
  -- current treatment and, via the confirmed patient_assignments.patient_id
  -- foreign key, conflicts with KEY SHARE locks taken by concurrent assignment
  -- inserts. The assignment rows themselves are locked below against removal
  -- or reassignment.
  perform 1
  from public.patients as patient
  where patient.user_id = p_requested_by
  for update;

  select count(*)
  into v_patient_count
  from public.patients as patient
  where patient.user_id = p_requested_by;

  if v_patient_count <> 1 then
    raise exception using
      errcode = '42501',
      message = 'The Patient record link is missing or ambiguous.';
  end if;

  select patient.patient_id, patient.treatment
  into v_patient_id, v_current_treatment
  from public.patients as patient
  where patient.user_id = p_requested_by
  for update;

  v_normalized_current_treatment := btrim(v_current_treatment);

  if v_normalized_treatment is not distinct from v_normalized_current_treatment then
    raise exception using
      errcode = '22023',
      message = 'The proposed treatment must differ from the current value.';
  end if;

  perform 1
  from public.patient_assignments as assignment
  where assignment.patient_id = v_patient_id
  for update;

  perform 1
  from public.users as reviewer
  where reviewer.user_id in (
    select assignment.staff_user_id
    from public.patient_assignments as assignment
    where assignment.patient_id = v_patient_id
  )
  for share;

  perform 1
  from public.permissions as permission
  where permission.role = 'Physician'
    and permission.action = 'review_escalation'
  for share;

  for v_candidate_reviewer_id in
    select distinct reviewer.user_id
    from public.patient_assignments as assignment
    join public.users as reviewer
      on reviewer.user_id = assignment.staff_user_id
    join public.permissions as permission
      on permission.role = 'Physician'
     and permission.action = 'review_escalation'
     and permission.allowed is true
    where assignment.patient_id = v_patient_id
      and reviewer.role = 'Physician'
      and reviewer.is_active is true
  loop
    v_reviewer_count := v_reviewer_count + 1;
    v_reviewer_id := v_candidate_reviewer_id;
  end loop;

  if v_reviewer_count <> 1 then
    raise exception using
      errcode = '42501',
      message = 'No unique active assigned Physician is authorized to review this request.';
  end if;

  insert into public.ai_requests (
    requested_by,
    patient_id,
    action,
    request_details,
    guardrail_decision,
    decision_reason
  )
  values (
    p_requested_by,
    v_patient_id,
    'patient_treatment_correction',
    jsonb_build_object(
      'field', 'treatment',
      'proposed_value', v_normalized_treatment
    )::text,
    'Escalate',
    'Human review required.'
  )
  returning request_id into v_request_id;

  insert into public.approvals (
    request_id,
    reviewer_id,
    status
  )
  values (
    v_request_id,
    v_reviewer_id,
    'Pending'
  );

  insert into public.audit_logs (
    request_id,
    user_id,
    event_type,
    details
  )
  values (
    v_request_id,
    p_requested_by,
    'patient.correction.submitted',
    'Patient correction request submitted for Physician review.'
  );

  return jsonb_build_object(
    'request_id', v_request_id,
    'guardrail_decision', 'Escalate',
    'approval_status', 'Pending'
  );
end;
$function$;

revoke all on function public.submit_patient_treatment_correction(integer, text)
  from public, anon, authenticated;
grant execute on function public.submit_patient_treatment_correction(integer, text)
  to service_role;

commit;
