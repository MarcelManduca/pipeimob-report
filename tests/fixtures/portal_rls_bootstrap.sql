-- ONLY for a fresh, disposable PostgreSQL service. Never run on Supabase.
-- Minimal pre-portal dependencies, not a production schema dump.
do $$
begin
  if current_database() <> 'gralha_rls_ci'
     or to_regclass('public.profiles') is not null then
    raise exception 'Expected empty disposable gralha_rls_ci database';
  end if;
end
$$;

create role anon nologin nosuperuser nobypassrls;
create role authenticated nologin nosuperuser nobypassrls;
create role service_role nologin nosuperuser bypassrls;
create schema auth;
grant usage on schema auth, public to anon, authenticated, service_role;

-- Emulates the DB identity claim only. This is NOT an Auth/JWT signature test.
create function auth.uid() returns uuid
language sql stable security invoker set search_path = ''
as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;

create table public.profiles (
  id uuid primary key,
  status text not null default 'active'
);
alter table public.profiles enable row level security;
-- Reproduce the audited broad grants before applying the proposal.
grant all on table public.profiles to anon, authenticated, service_role;

create table public.user_roles (user_id uuid, role text);
create table public.sales_team_reference (
  team_key text, team_name text, source_updated_through timestamptz
);
create table public.manager_team_reference (
  team_key text, team_name text, source_updated_through timestamptz
);
