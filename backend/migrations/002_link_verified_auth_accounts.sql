-- Run only after the corresponding accounts exist in Supabase Auth and have
-- verified email addresses. This links unique exact-email matches only.
-- Existing public.users records and roles are preserved.
with eligible_users as (
  select user_id, lower(btrim(email)) as normalized_email
  from public.users
  where auth_user_id is null
    and btrim(email) <> ''
    and lower(btrim(email)) in (
      select lower(btrim(email))
      from public.users
      where auth_user_id is null
        and btrim(email) <> ''
      group by lower(btrim(email))
      having count(*) = 1
    )
),
eligible_auth_accounts as (
  select id as auth_user_id, lower(btrim(email)) as normalized_email
  from auth.users
  where email_confirmed_at is not null
    and email is not null
    and btrim(email) <> ''
    and not exists (
      select 1
      from public.users as linked_users
      where linked_users.auth_user_id = auth.users.id
    )
    and lower(btrim(email)) in (
      select lower(btrim(email))
      from auth.users
      where email_confirmed_at is not null
        and email is not null
        and btrim(email) <> ''
      group by lower(btrim(email))
      having count(*) = 1
    )
),
eligible_links as (
  select users.user_id, auth_accounts.auth_user_id
  from eligible_users as users
  join eligible_auth_accounts as auth_accounts
    using (normalized_email)
)
update public.users as users
set auth_user_id = links.auth_user_id
from eligible_links as links
where users.user_id = links.user_id
  and users.auth_user_id is null;
