-- Add an account status flag without deleting or rewriting existing users.
-- Existing application accounts remain active by default.
alter table public.users
  add column if not exists is_active boolean not null default true;
