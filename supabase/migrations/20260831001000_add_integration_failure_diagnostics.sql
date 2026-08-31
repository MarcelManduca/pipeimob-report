-- Sanitized operational diagnostics for authenticated internal integrations.
-- This table never stores tokens, request/response bodies, customer data or
-- commercial facts. It exists only to identify the failing integration layer.

create table public.integration_failure_diagnostics (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid(),
  occurred_at timestamptz not null default now(),
  function_slug text not null,
  function_version text not null,
  operation text not null,
  period_start date,
  period_end date,
  http_status smallint not null,
  error_code text not null,
  constraint integration_failure_diagnostics_status_range
    check (http_status between 0 and 599),
  constraint integration_failure_diagnostics_function_slug_not_blank
    check (btrim(function_slug) <> ''),
  constraint integration_failure_diagnostics_function_version_not_blank
    check (btrim(function_version) <> ''),
  constraint integration_failure_diagnostics_operation_not_blank
    check (btrim(operation) <> ''),
  constraint integration_failure_diagnostics_error_code_format
    check (error_code ~ '^[a-z0-9][a-z0-9_:-]{0,79}$'),
  constraint integration_failure_diagnostics_period_order
    check (period_start is null or period_end is null or period_end >= period_start)
);

comment on table public.integration_failure_diagnostics is
  'Sanitized internal integration failures; excludes tokens, payloads, customer data and commercial facts.';

create index integration_failure_diagnostics_recent_idx
  on public.integration_failure_diagnostics (occurred_at desc);

alter table public.integration_failure_diagnostics enable row level security;

revoke all on table public.integration_failure_diagnostics from anon, authenticated;
grant insert on table public.integration_failure_diagnostics to authenticated;

create policy integration_failure_diagnostics_insert_authorized
on public.integration_failure_diagnostics
for insert
to authenticated
with check (
  user_id = (select auth.uid())
  and exists (
    select 1
    from public.profiles as profile
    join public.user_roles as user_role
      on user_role.user_id = profile.id
    where profile.id = (select auth.uid())
      and profile.status = 'active'
      and user_role.role::text in ('super_admin', 'viewer')
  )
);
