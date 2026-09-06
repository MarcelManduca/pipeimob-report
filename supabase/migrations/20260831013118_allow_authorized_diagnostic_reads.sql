-- Allow active internal roles to read sanitized integration health signals.
-- Rows contain only operational metadata and never tokens, payloads, customer
-- data or commercial facts.

create index if not exists integration_failure_diagnostics_operation_recent_idx
  on public.integration_failure_diagnostics (operation, occurred_at desc);

grant select on table public.integration_failure_diagnostics to authenticated;

create policy integration_failure_diagnostics_select_authorized
on public.integration_failure_diagnostics
for select
to authenticated
using (
  exists (
    select 1
    from public.profiles as profile
    join public.user_roles as user_role
      on user_role.user_id = profile.id
    where profile.id = (select auth.uid())
      and profile.status = 'active'
      and user_role.role::text in ('super_admin', 'viewer')
  )
);
