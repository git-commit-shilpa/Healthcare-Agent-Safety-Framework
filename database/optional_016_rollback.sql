-- Rollback for optional_016: restore the 015 submit_patient_treatment_correction (one open correction per Patient).
-- Existing requests are untouched. Run only if 016 was applied.
begin;

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
  v_assignee integer;
  v_task_id integer;
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

  -- Duplicate prevention. Every submission for this Patient already holds the
  -- FOR UPDATE lock on the Patient row taken above, so concurrent submissions
  -- serialize here. Under READ COMMITTED this statement takes a fresh snapshot
  -- after the lock is granted and therefore sees a request committed by the
  -- transaction that held the lock. A request blocks a new one unless it has
  -- exactly one approval that is Approved or Rejected (Pending, missing,
  -- duplicated, or unrecognized approvals all block).
  if exists (
    select 1
    from public.ai_requests as existing_request
    where existing_request.patient_id = v_patient_id
      and existing_request.action = 'patient_treatment_correction'
      and not (
        (
          (
            select count(*)
            from public.approvals as existing_approval
            where existing_approval.request_id = existing_request.request_id
          ) = 1
          and exists (
            select 1
            from public.approvals as final_approval
            where final_approval.request_id = existing_request.request_id
              and final_approval.status in ('Approved', 'Rejected')
          )
        )
        -- 015: a correction the care team declined before any Physician approval existed is closed.
        or (
          not exists (
            select 1 from public.approvals as no_approval
            where no_approval.request_id = existing_request.request_id
          )
          and exists (
            select 1 from public.technician_tasks as closed_task
            where closed_task.approval_request_id = existing_request.request_id
              and closed_task.category = 'record_correction'
              and closed_task.status in ('Declined', 'Cancelled')
          )
        )
      )
  ) then
    -- PostgREST maps SQLSTATE PTxyz to HTTP status xyz.
    raise exception using
      errcode = 'PT409',
      message = 'A treatment correction request is already awaiting review.';
  end if;

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

  -- Technician first (015). Fail closed BEFORE anything is written: with no eligible Technician the request is
  -- refused; it is never routed straight to the Physician. Same Technician choice as submit_service_request (012):
  -- serialized by the same advisory lock, fewest open tasks, lowest user_id.
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('hasf.technician_task_assignment', 0)
  );

  perform 1 from public.permissions as permission
  where permission.role = 'Technician'
    and permission.action in ('view_assigned_task', 'update_task_status')
  for share;

  select candidate.user_id into v_assignee
  from public.patient_assignments as assignment
  join public.users as candidate
    on candidate.user_id = assignment.staff_user_id
  where assignment.patient_id = v_patient_id
    and candidate.role = 'Technician'
    and candidate.is_active is true
    and (
      select count(*) from public.permissions as permission
      where permission.role = 'Technician'
        and permission.action in ('view_assigned_task', 'update_task_status')
        and permission.allowed is true
    ) = 2
  order by
    (select count(*) from public.technician_tasks as open_task
      where open_task.assigned_to = candidate.user_id
        and open_task.status not in ('Completed', 'Declined', 'Cancelled')),
    candidate.user_id
  limit 1;

  if v_assignee is null then
    raise exception using
      errcode = 'PT422',
      message = 'No technician is currently available for your care team. Please contact your care team.',
      detail = 'no_eligible_technician';
  end if;

  -- One request, NO approval: the Physician sees nothing until the assigned Technician escalates.
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

  -- No correction text here: a fixed title, no description.
  insert into public.technician_tasks (
    patient_id, requested_by, assigned_to, category, title, description,
    status, requires_physician_approval, approval_request_id
  ) values (
    v_patient_id, p_requested_by, v_assignee, 'record_correction',
    'Record correction request #' || v_request_id::text, null,
    'Submitted', true, v_request_id
  )
  returning task_id into v_task_id;

  insert into public.technician_task_events (
    task_id, actor_user_id, actor_role, event_type, to_status,
    guardrail_decision, guardrail_reason, approval_request_id
  ) values (
    v_task_id, p_requested_by, 'Patient', 'created', 'Submitted',
    'Escalate', 'record_correction_requires_technician_review', v_request_id
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
    'Patient correction request submitted for care-team review.'
  );

  insert into public.audit_logs (request_id, user_id, event_type, details)
  values (
    v_request_id, p_requested_by, 'patient.correction.coordination_created',
    jsonb_build_object('task_id', v_task_id, 'request_id', v_request_id)::text
  );

  return jsonb_build_object(
    'request_id', v_request_id,
    'guardrail_decision', 'Escalate',
    'approval_status', null,
    'review_stage', 'with_care_team',
    'coordination', 'assigned',
    'task_id', v_task_id
  );
end;
$function$;

revoke all on function public.submit_patient_treatment_correction(integer, text)
  from public, anon, authenticated;
grant execute on function public.submit_patient_treatment_correction(integer, text)
  to service_role;

commit;
