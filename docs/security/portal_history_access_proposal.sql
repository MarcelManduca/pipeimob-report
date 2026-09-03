-- PROPOSAL ONLY: not an automatically applied migration.
-- See ../GRALHA_HISTORY_ACCESS_HARDENING.md before generating a migration.
-- Target: the existing portal schema, not a new Supabase project.
begin;

-- RLS is not a replacement for grants (e.g. TRUNCATE is outside RLS).
-- PostgreSQL also revokes corresponding column grants with table REVOKE.
-- Preserve the existing service_role grants and profiles SELECT policies.
alter table public.profiles enable row level security;
revoke all privileges on table public.profiles from public, anon, authenticated;

grant select on table public.profiles to authenticated;

alter table public.conversations enable row level security;
alter table public.conversation_messages enable row level security;

-- RESTRICTIVE means AND with the existing ownership policies, not OR.
-- Current profiles are read under the caller's RLS; no privileged helper or
-- stale/user-editable JWT role/status claims are introduced.
create policy conversations_require_active_profile
on public.conversations
as restrictive
for all
to authenticated
using (
  user_id = (select auth.uid())
  and exists (
    select 1 from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.status::text = 'active'
      and profile.access_role in ('ceo', 'cso', 'cmo', 'store_director', 'team_manager')
  )
)
with check (
  user_id = (select auth.uid())
  and exists (
    select 1 from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.status::text = 'active'
      and profile.access_role in ('ceo', 'cso', 'cmo', 'store_director', 'team_manager')
  )
);

create policy conversation_messages_require_active_profile
on public.conversation_messages
as restrictive
for all
to authenticated
using (
  user_id = (select auth.uid())
  and exists (
    select 1 from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.status::text = 'active'
      and profile.access_role in ('ceo', 'cso', 'cmo', 'store_director', 'team_manager')
  )
)
with check (
  user_id = (select auth.uid())
  and exists (
    select 1 from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.status::text = 'active'
      and profile.access_role in ('ceo', 'cso', 'cmo', 'store_director', 'team_manager')
  )
);

commit;
