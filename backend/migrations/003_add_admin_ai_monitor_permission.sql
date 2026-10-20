-- Add the explicit Admin permission needed to view safe AI-request metadata.
-- This inserts one row only when the same permission does not already exist.
-- It does not update or delete any existing permission or application record.
insert into public.permissions (role, action, allowed)
select 'Admin', 'monitor_ai_requests', true
where not exists (
  select 1
  from public.permissions
  where role = 'Admin'
    and action = 'monitor_ai_requests'
);
