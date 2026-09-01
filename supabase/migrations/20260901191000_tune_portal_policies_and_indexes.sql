drop policy if exists "Users and superadmins can view profiles" on public.profiles;

create index if not exists user_management_audit_actor_idx
  on public.user_management_audit (actor_user_id, created_at desc);

create index if not exists user_team_access_created_by_idx
  on public.user_team_access (created_by)
  where created_by is not null;
