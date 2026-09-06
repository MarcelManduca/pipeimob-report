-- TEST CANDIDATE ONLY. Not a migration or a production authorization.
-- Tables/types are supplied by the historical schema in the isolated harness.
do $$ begin
  if current_database() <> 'gralha_bootstrap_ci' then
    raise exception 'Only disposable gralha_bootstrap_ci is allowed';
  end if;
end $$;

create or replace function public.sync_validation_user_access()
returns trigger language plpgsql security definer set search_path = ''
as $$
declare
  normalized_email text := lower(nullif(btrim(NEW.email), ''));
  resolved_status public.profile_status;
begin
  -- The portal is email-based; support for phone/anonymous users is not decided.
  if normalized_email is null then
    raise exception 'Portal profile requires an email';
  end if;
  resolved_status := case when NEW.email_confirmed_at is not null
    then 'active'::public.profile_status else 'invited'::public.profile_status end;

  insert into public.profiles(id,email,status,status_updated_at,activated_at)
  values (NEW.id,normalized_email,resolved_status,now(),
    case when resolved_status='active' then now() else null end)
  on conflict (id) do update set
    email=excluded.email,
    status=case when public.profiles.status='disabled'
      then 'disabled'::public.profile_status else excluded.status end,
    status_updated_at=now(),
    activated_at=coalesce(public.profiles.activated_at,excluded.activated_at);

  -- Existing roles are administrative decisions, not derived from email.
  insert into public.user_roles(user_id,role) values (NEW.id,'viewer')
  on conflict (user_id) do nothing;
  return NEW;
end;
$$;
revoke all on function public.sync_validation_user_access() from public,anon,authenticated;

drop trigger if exists on_validation_auth_user_access on auth.users;
create trigger on_validation_auth_user_access
after insert or update of email,email_confirmed_at on auth.users
for each row execute function public.sync_validation_user_access();

-- Candidate fresh-baseline fill: insert missing rows only; never reset decisions.
-- This still needs an explicit decision before becoming any production operation.
insert into public.profiles(id,email,status,status_updated_at,activated_at)
select id,lower(btrim(email)),
  case when email_confirmed_at is not null then 'active'::public.profile_status
    else 'invited'::public.profile_status end,
  now(),case when email_confirmed_at is not null then now() else null end
from auth.users where nullif(btrim(email),'') is not null
on conflict (id) do nothing;

insert into public.user_roles(user_id,role)
select id,'viewer'::public.app_role from public.profiles
on conflict (user_id) do nothing;
