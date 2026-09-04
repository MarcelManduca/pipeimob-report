-- TEST CANDIDATE ONLY. Not a migration or a production authorization.
-- Fresh identity/RBAC baseline: no privileged email and no user backfill.
do $$ begin
  if current_database() <> 'gralha_baseline_ci' then
    raise exception 'Only disposable gralha_baseline_ci is allowed';
  end if;
end $$;

create schema if not exists private;

-- Prevent future project objects from inheriting broad API grants.
alter default privileges for role postgres in schema public
  revoke all on tables from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke all on sequences from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke execute on functions from public, anon, authenticated;

create type public.app_role as enum ('super_admin', 'viewer');
create type public.profile_status as enum ('invited', 'active', 'disabled');

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null unique,
  status public.profile_status not null default 'invited',
  created_at timestamptz not null default now(),
  status_updated_at timestamptz not null default now(),
  activated_at timestamptz,
  disabled_at timestamptz,
  display_name text,
  access_role text not null default 'team_manager',
  updated_at timestamptz not null default now(),
  constraint profiles_access_role_valid check (
    access_role in ('ceo', 'cso', 'cmo', 'store_director', 'team_manager')
  )
);

create table public.user_roles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique
    references public.profiles(id) on delete cascade,
  role public.app_role not null,
  created_at timestamptz not null default now()
);

create or replace function public.has_role(
  _user_id uuid,
  _role public.app_role
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.user_roles as user_role
    join public.profiles as profile on profile.id = user_role.user_id
    where user_role.user_id = _user_id
      and user_role.role = _role
      and profile.status = 'active'
  )
$$;

create or replace function public.is_super_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select public.has_role((select auth.uid()), 'super_admin'::public.app_role)
$$;

create or replace function public.get_my_role()
returns public.app_role
language sql
stable
security definer
set search_path = ''
as $$
  select user_role.role
  from public.user_roles as user_role
  join public.profiles as profile on profile.id = user_role.user_id
  where user_role.user_id = (select auth.uid())
    and profile.status = 'active'
  limit 1
$$;

create or replace function public.is_active_user(_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.profiles
    where id = _user_id and status = 'active'
  )
$$;

create or replace function public.is_active_super_admin(_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select public.has_role(_user_id, 'super_admin'::public.app_role)
$$;

create or replace function private.current_user_is_executive()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.status = 'active'
      and profile.access_role in ('ceo', 'cso', 'cmo')
  )
$$;

create or replace function public.sync_validation_user_access()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  normalized_email text := lower(nullif(btrim(new.email), ''));
  resolved_status public.profile_status;
begin
  if normalized_email is null then
    raise exception 'Portal profile requires an email';
  end if;

  resolved_status := case
    when new.email_confirmed_at is not null then 'active'::public.profile_status
    else 'invited'::public.profile_status
  end;

  insert into public.profiles (
    id, email, status, status_updated_at, activated_at
  )
  values (
    new.id,
    normalized_email,
    resolved_status,
    now(),
    case when resolved_status = 'active' then now() else null end
  )
  on conflict (id) do update
  set email = excluded.email,
      status = case
        when public.profiles.status = 'disabled'
          then 'disabled'::public.profile_status
        else excluded.status
      end,
      status_updated_at = now(),
      activated_at = coalesce(
        public.profiles.activated_at,
        excluded.activated_at
      );

  -- Roles and portal scope are explicit administrative decisions.
  insert into public.user_roles (user_id, role)
  values (new.id, 'viewer')
  on conflict (user_id) do nothing;

  return new;
end
$$;

create trigger on_validation_auth_user_access
after insert or update of email, email_confirmed_at on auth.users
for each row execute function public.sync_validation_user_access();

revoke all on function public.has_role(uuid, public.app_role)
  from public, anon, authenticated, service_role;
revoke all on function public.is_super_admin()
  from public, anon, authenticated, service_role;
revoke all on function public.get_my_role()
  from public, anon, authenticated, service_role;
revoke all on function public.is_active_user(uuid)
  from public, anon, authenticated, service_role;
revoke all on function public.is_active_super_admin(uuid)
  from public, anon, authenticated, service_role;
revoke all on function private.current_user_is_executive()
  from public, anon, authenticated, service_role;
revoke all on function public.sync_validation_user_access()
  from public, anon, authenticated, service_role;

grant execute on function public.has_role(uuid, public.app_role)
  to authenticated, service_role;
grant execute on function public.is_super_admin()
  to authenticated, service_role;
grant execute on function public.get_my_role()
  to authenticated, service_role;
grant execute on function public.is_active_user(uuid)
  to authenticated, service_role;
grant execute on function public.is_active_super_admin(uuid)
  to authenticated, service_role;
grant usage on schema private to authenticated;
grant execute on function private.current_user_is_executive() to authenticated;

alter table public.profiles enable row level security;
alter table public.user_roles enable row level security;

revoke all on table public.profiles from public, anon, authenticated;
revoke all on table public.user_roles from public, anon, authenticated;
grant select on table public.profiles to authenticated;
grant select on table public.user_roles to authenticated;
grant all on table public.profiles to service_role;
grant all on table public.user_roles to service_role;

create policy profiles_select_self_or_executive
on public.profiles for select to authenticated
using (
  id = (select auth.uid())
  or (select private.current_user_is_executive())
);

create policy "Users and superadmins can view roles"
on public.user_roles for select to authenticated
using (
  user_id = (select auth.uid())
  or (select public.is_super_admin())
);
