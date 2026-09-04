-- TEST CANDIDATE ONLY. Final grants for the disposable full-baseline replay.
do $$ begin
  if current_database() <> 'gralha_baseline_ci' then
    raise exception 'Only disposable gralha_baseline_ci is allowed';
  end if;
end $$;

revoke all on all tables in schema public from public, anon, authenticated;
revoke all on all sequences in schema public from public, anon, authenticated;

grant select on table public.profiles to authenticated;
grant select on table public.user_roles to authenticated;
grant select on table public.internal_sales_spreadsheet_rows to authenticated;
grant select on table public.sales_team_reference to authenticated;
grant select on table public.manager_team_reference to authenticated;
grant select, insert on table public.integration_failure_diagnostics to authenticated;
grant select on table public.teams to authenticated;
grant select on table public.user_team_access to authenticated;
grant select, insert, update, delete on table public.conversations to authenticated;
grant select, insert, delete on table public.conversation_messages to authenticated;
grant usage, select on sequence public.conversation_messages_id_seq to authenticated;

grant all on all tables in schema public to service_role;
grant usage, select, update on all sequences in schema public to service_role;

revoke all on schema validation from public, anon, authenticated, service_role;
revoke all on all tables in schema validation
  from public, anon, authenticated, service_role;
revoke all on all sequences in schema validation
  from public, anon, authenticated, service_role;

revoke all on function public.sync_validation_user_access()
  from public, anon, authenticated, service_role;
revoke all on function public.get_validation_sales_reconciliation(date, date)
  from public, anon, authenticated, service_role;
grant execute on function public.get_validation_sales_reconciliation(date, date)
  to service_role;
