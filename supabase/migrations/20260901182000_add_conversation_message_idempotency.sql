alter table public.conversation_messages
  add column if not exists request_id uuid;

create unique index if not exists conversation_messages_request_role_uidx
  on public.conversation_messages (conversation_id, role, request_id)
  where request_id is not null;

comment on column public.conversation_messages.request_id is
  'Client-generated idempotency key used to prevent duplicate messages after safe retries.';
