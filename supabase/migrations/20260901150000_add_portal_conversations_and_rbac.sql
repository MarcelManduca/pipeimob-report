-- Gralha Indicadores: persistent conversations and role/team access model.
-- Business facts remain in the live Pipeimob and Vista integrations.

create schema if not exists private;

alter table public.profiles
  add column if not exists display_name text,
  add column if not exists access_role text,
  add column if not exists updated_at timestamptz not null default now();

update public.profiles as profile
set access_role = case
  when exists (
    select 1
    from public.user_roles as user_role
    where user_role.user_id = profile.id
      and user_role.role::text = 'super_admin'
  ) then 'cmo'
  else 'team_manager'
end
where access_role is null;

alter table public.profiles
  alter column access_role set default 'team_manager',
  alter column access_role set not null;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'profiles_access_role_valid'
      and conrelid = 'public.profiles'::regclass
  ) then
    alter table public.profiles
      add constraint profiles_access_role_valid
      check (access_role in (
        'ceo',
        'cso',
        'cmo',
        'store_director',
        'team_manager'
      ));
  end if;
end
$$;

create table if not exists public.teams (
  team_key text primary key,
  team_name text not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint teams_key_not_blank check (btrim(team_key) <> ''),
  constraint teams_name_not_blank check (btrim(team_name) <> '')
);

insert into public.teams (team_key, team_name)
select distinct on (source.team_key)
  source.team_key,
  source.team_name
from (
  select team_key, team_name, source_updated_through
  from public.sales_team_reference
  union all
  select team_key, team_name, source_updated_through
  from public.manager_team_reference
) as source
where nullif(btrim(source.team_key), '') is not null
  and nullif(btrim(source.team_name), '') is not null
order by source.team_key, source.source_updated_through desc nulls last
on conflict (team_key) do update
set team_name = excluded.team_name,
    updated_at = now();

create table if not exists public.user_team_access (
  user_id uuid not null references public.profiles(id) on delete cascade,
  team_key text not null references public.teams(team_key) on update cascade,
  created_at timestamptz not null default now(),
  created_by uuid references public.profiles(id) on delete set null,
  primary key (user_id, team_key)
);

create index if not exists user_team_access_team_key_idx
  on public.user_team_access (team_key, user_id);

create table if not exists public.conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  title text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint conversations_title_not_blank check (btrim(title) <> ''),
  constraint conversations_title_length check (char_length(title) <= 160)
);

create index if not exists conversations_user_updated_idx
  on public.conversations (user_id, updated_at desc, id);

create table if not exists public.conversation_messages (
  id bigint generated always as identity primary key,
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  role text not null,
  content text not null,
  visualization jsonb,
  created_at timestamptz not null default now(),
  constraint conversation_messages_role_valid
    check (role in ('user', 'assistant')),
  constraint conversation_messages_content_not_blank
    check (btrim(content) <> ''),
  constraint conversation_messages_content_length
    check (char_length(content) <= 12000),
  constraint conversation_messages_visualization_object
    check (visualization is null or jsonb_typeof(visualization) = 'object')
);

create index if not exists conversation_messages_conversation_cursor_idx
  on public.conversation_messages (conversation_id, id);

create index if not exists conversation_messages_user_id_idx
  on public.conversation_messages (user_id);

create table if not exists public.user_management_audit (
  id bigint generated always as identity primary key,
  actor_user_id uuid references public.profiles(id) on delete set null,
  target_user_id uuid references public.profiles(id) on delete set null,
  action text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint user_management_audit_action_not_blank
    check (btrim(action) <> ''),
  constraint user_management_audit_details_object
    check (jsonb_typeof(details) = 'object')
);

create index if not exists user_management_audit_created_idx
  on public.user_management_audit (created_at desc, id);

create index if not exists user_management_audit_target_idx
  on public.user_management_audit (target_user_id, created_at desc);

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
      and profile.status::text = 'active'
      and profile.access_role in ('ceo', 'cso', 'cmo')
  )
$$;

revoke all on function private.current_user_is_executive()
  from public, anon, authenticated, service_role;

alter table public.teams enable row level security;
alter table public.user_team_access enable row level security;
alter table public.conversations enable row level security;
alter table public.conversation_messages enable row level security;
alter table public.user_management_audit enable row level security;

revoke all on table public.teams from anon, authenticated;
revoke all on table public.user_team_access from anon, authenticated;
revoke all on table public.conversations from anon, authenticated;
revoke all on table public.conversation_messages from anon, authenticated;
revoke all on table public.user_management_audit from anon, authenticated;

grant select on table public.teams to authenticated;
grant select on table public.user_team_access to authenticated;
grant select, insert, update, delete on table public.conversations to authenticated;
grant select, insert, delete on table public.conversation_messages to authenticated;
grant usage, select on sequence public.conversation_messages_id_seq to authenticated;

grant all on table public.teams to service_role;
grant all on table public.user_team_access to service_role;
grant all on table public.conversations to service_role;
grant all on table public.conversation_messages to service_role;
grant all on table public.user_management_audit to service_role;
grant usage, select on sequence public.conversation_messages_id_seq to service_role;
grant usage, select on sequence public.user_management_audit_id_seq to service_role;

drop policy if exists teams_select_authorized on public.teams;
create policy teams_select_authorized
on public.teams
for select
to authenticated
using (
  exists (
    select 1
    from public.profiles as profile
    where profile.id = (select auth.uid())
      and profile.status::text = 'active'
      and (
        profile.access_role in ('ceo', 'cso', 'cmo')
        or exists (
          select 1
          from public.user_team_access as access
          where access.user_id = profile.id
            and access.team_key = teams.team_key
        )
      )
  )
);

drop policy if exists user_team_access_select_scope on public.user_team_access;
create policy user_team_access_select_scope
on public.user_team_access
for select
to authenticated
using (
  user_id = (select auth.uid())
  or (select private.current_user_is_executive())
);

drop policy if exists conversations_select_own on public.conversations;
create policy conversations_select_own
on public.conversations
for select
to authenticated
using (user_id = (select auth.uid()));

drop policy if exists conversations_insert_own on public.conversations;
create policy conversations_insert_own
on public.conversations
for insert
to authenticated
with check (user_id = (select auth.uid()));

drop policy if exists conversations_update_own on public.conversations;
create policy conversations_update_own
on public.conversations
for update
to authenticated
using (user_id = (select auth.uid()))
with check (user_id = (select auth.uid()));

drop policy if exists conversations_delete_own on public.conversations;
create policy conversations_delete_own
on public.conversations
for delete
to authenticated
using (user_id = (select auth.uid()));

drop policy if exists conversation_messages_select_own on public.conversation_messages;
create policy conversation_messages_select_own
on public.conversation_messages
for select
to authenticated
using (
  user_id = (select auth.uid())
  and exists (
    select 1
    from public.conversations as conversation
    where conversation.id = conversation_messages.conversation_id
      and conversation.user_id = (select auth.uid())
  )
);

drop policy if exists conversation_messages_insert_own on public.conversation_messages;
create policy conversation_messages_insert_own
on public.conversation_messages
for insert
to authenticated
with check (
  user_id = (select auth.uid())
  and exists (
    select 1
    from public.conversations as conversation
    where conversation.id = conversation_messages.conversation_id
      and conversation.user_id = (select auth.uid())
  )
);

drop policy if exists conversation_messages_delete_own on public.conversation_messages;
create policy conversation_messages_delete_own
on public.conversation_messages
for delete
to authenticated
using (
  user_id = (select auth.uid())
  and exists (
    select 1
    from public.conversations as conversation
    where conversation.id = conversation_messages.conversation_id
      and conversation.user_id = (select auth.uid())
  )
);

drop policy if exists profiles_select_self_or_executive on public.profiles;
create policy profiles_select_self_or_executive
on public.profiles
for select
to authenticated
using (
  id = (select auth.uid())
  or (select private.current_user_is_executive())
);

drop policy if exists user_management_audit_select_executive
  on public.user_management_audit;
create policy user_management_audit_select_executive
on public.user_management_audit
for select
to authenticated
using ((select private.current_user_is_executive()));

comment on table public.conversations is
  'Private per-user Gralha Indicadores conversation history.';
comment on table public.conversation_messages is
  'Visible chat messages only; never stores access tokens or raw integration payloads.';
comment on table public.user_team_access is
  'Date-independent portal authorization scope. Commercial attribution remains separate.';
