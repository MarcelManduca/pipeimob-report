-- Management interpretation only. Official sales facts (count, date and VGV)
-- continue to come from the live Pipeimob API and commercial ownership from
-- the live Vista API.

create table public.manager_team_reference (
  id bigint generated always as identity primary key,
  manager_key text not null,
  manager_name text not null,
  team_key text not null,
  team_name text not null,
  valid_from date not null,
  valid_to date,
  source_file_name text not null,
  source_sheet_name text not null,
  source_updated_through date,
  evidence_rows integer not null,
  review_required boolean not null default false,
  resolution_method text not null default 'management_spreadsheet_effective_date',
  created_at timestamptz not null default now(),
  constraint manager_team_reference_manager_key_not_blank
    check (btrim(manager_key) <> ''),
  constraint manager_team_reference_manager_name_not_blank
    check (btrim(manager_name) <> ''),
  constraint manager_team_reference_team_key_not_blank
    check (btrim(team_key) <> ''),
  constraint manager_team_reference_team_name_not_blank
    check (btrim(team_name) <> ''),
  constraint manager_team_reference_valid_period
    check (valid_to is null or valid_to >= valid_from),
  constraint manager_team_reference_positive_evidence
    check (evidence_rows > 0),
  constraint manager_team_reference_natural_key
    unique (manager_key, team_key, valid_from)
);

comment on table public.manager_team_reference is
  'Date-aware manager-to-team interpretation derived from the management spreadsheet; never an official sales fact source.';

create index manager_team_reference_lookup_idx
  on public.manager_team_reference (manager_key, valid_from desc, valid_to);

create index manager_team_reference_source_coverage_idx
  on public.manager_team_reference (source_updated_through desc);

alter table public.manager_team_reference enable row level security;

revoke all on table public.manager_team_reference from anon, authenticated;
grant select on table public.manager_team_reference to authenticated;

create policy manager_team_reference_select_authorized
on public.manager_team_reference
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

-- Collapse the spreadsheet's sale-level rows into a small, date-effective
-- management dimension. A same-day tie is excluded instead of guessed.
with observations as (
  select
    row.sale_date,
    btrim(row.row_data ->> 'Gerente') as manager_name,
    btrim(row.row_data ->> 'Equipe') as team_name,
    row.source_file_name,
    row.source_sheet_name,
    row.source_updated_through
  from public.internal_sales_spreadsheet_rows as row
  where row.sale_date is not null
    and nullif(btrim(row.row_data ->> 'Gerente'), '') is not null
    and nullif(btrim(row.row_data ->> 'Equipe'), '') is not null
),
normalized as (
  select
    sale_date,
    lower(regexp_replace(translate(
      manager_name,
      'ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇáàâãäéèêëíìîïóòôõöúùûüç',
      'AAAAAEEEEIIIIOOOOOUUUUCaaaaaeeeeiiiiooooouuuuc'
    ), '\s+', ' ', 'g'))
      as manager_key,
    manager_name,
    lower(regexp_replace(translate(
      team_name,
      'ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇáàâãäéèêëíìîïóòôõöúùûüç',
      'AAAAAEEEEIIIIOOOOOUUUUCaaaaaeeeeiiiiooooouuuuc'
    ), '\s+', ' ', 'g'))
      as team_key,
    team_name,
    source_file_name,
    source_sheet_name,
    source_updated_through
  from observations
),
daily_counts as (
  select
    manager_key,
    sale_date,
    team_key,
    (array_agg(manager_name order by manager_name))[1] as manager_name,
    (array_agg(team_name order by team_name))[1] as team_name,
    max(source_file_name) as source_file_name,
    max(source_sheet_name) as source_sheet_name,
    max(source_updated_through) as source_updated_through,
    count(*)::integer as evidence_rows
  from normalized
  group by manager_key, sale_date, team_key
),
daily_ranked as (
  select
    daily_counts.*,
    row_number() over (
      partition by manager_key, sale_date
      order by evidence_rows desc, team_key
    ) as position,
    lead(evidence_rows) over (
      partition by manager_key, sale_date
      order by evidence_rows desc, team_key
    ) as runner_up_rows
  from daily_counts
),
daily_choice as (
  select *
  from daily_ranked
  where position = 1
    and (runner_up_rows is null or evidence_rows > runner_up_rows)
),
marked as (
  select
    daily_choice.*,
    case
      when lag(team_key) over (
        partition by manager_key order by sale_date
      ) is distinct from team_key then 1
      else 0
    end as starts_new_segment
  from daily_choice
),
grouped as (
  select
    marked.*,
    sum(starts_new_segment) over (
      partition by manager_key order by sale_date
    ) as segment_number
  from marked
),
segments as (
  select
    manager_key,
    team_key,
    segment_number,
    (array_agg(manager_name order by sale_date desc))[1] as manager_name,
    (array_agg(team_name order by sale_date desc))[1] as team_name,
    min(sale_date) as valid_from,
    max(source_file_name) as source_file_name,
    max(source_sheet_name) as source_sheet_name,
    max(source_updated_through) as source_updated_through,
    sum(evidence_rows)::integer as evidence_rows
  from grouped
  group by manager_key, team_key, segment_number
),
manager_stats as (
  select
    manager_key,
    count(distinct team_key) > 1 as review_required
  from segments
  group by manager_key
),
dated_segments as (
  select
    segments.*,
    lead(valid_from) over (
      partition by manager_key order by valid_from, segment_number
    ) as next_valid_from,
    manager_stats.review_required
  from segments
  join manager_stats using (manager_key)
)
insert into public.manager_team_reference (
  manager_key,
  manager_name,
  team_key,
  team_name,
  valid_from,
  valid_to,
  source_file_name,
  source_sheet_name,
  source_updated_through,
  evidence_rows,
  review_required
)
select
  manager_key,
  manager_name,
  team_key,
  team_name,
  valid_from,
  next_valid_from - 1,
  source_file_name,
  source_sheet_name,
  source_updated_through,
  evidence_rows,
  review_required
from dated_segments;
