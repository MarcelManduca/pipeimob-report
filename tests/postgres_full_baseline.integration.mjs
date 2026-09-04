import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const container = process.env.GRALHA_BASELINE_TEST_CONTAINER || '';
if (process.env.GITHUB_ACTIONS !== 'true' ||
    process.env.GRALHA_ALLOW_DISPOSABLE_BASELINE_TESTS !== 'true' ||
    !/^[0-9a-f]{12,64}$/.test(container)) {
  throw new Error('Requires the explicitly enabled disposable baseline service');
}

const inspect = spawnSync('docker', ['inspect', container], {
  encoding: 'utf8',
  timeout: 10000,
});
assert.equal(inspect.status, 0);
const metadata = JSON.parse(inspect.stdout)[0];
assert.equal(metadata.Config.Image, 'postgres:17.11-bookworm');
assert.equal(Object.keys(metadata.HostConfig.PortBindings || {}).length, 0);
assert.ok(metadata.Config.Env.includes('POSTGRES_DB=gralha_baseline_ci'));

const file = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
function sql(text) {
  return spawnSync('docker', [
    'exec', '-i', container, 'psql', '-X', '-qAt',
    '-h', '/var/run/postgresql', '-U', 'postgres', '-d', 'gralha_baseline_ci',
    '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=sqlstate',
  ], {
    input: text,
    encoding: 'utf8',
    timeout: 20000,
    maxBuffer: 2 * 1024 * 1024,
  });
}
function ok(text) {
  const result = sql(text);
  assert.equal(
    result.status,
    0,
    `SQL failure: ${result.stderr.trim() || result.error?.message}`,
  );
  return result.stdout.trim();
}
function tx(text) {
  return ok(`begin; set local statement_timeout='5s'; ${text}; rollback;`);
}

const baselinePath =
  'docs/migrations/baseline-candidate/' +
  '20260904150012_establish_sanitized_schema_baseline.sql';
const baseline = file(baselinePath);
assert.doesNotMatch(
  baseline,
  /[a-z0-9._%+-]+@(?!example\.test)[a-z0-9.-]+\.[a-z]{2,}/i,
);
assert.doesNotMatch(
  baseline,
  /eyJ[A-Za-z0-9_-]{20,}|sb_secret_|sbp_[A-Za-z0-9]{20,}/,
);

assert.equal(ok('select current_database()'), 'gralha_baseline_ci');
ok(`
  create role anon nologin nosuperuser nobypassrls;
  create role authenticated nologin nosuperuser nobypassrls;
  create role service_role nologin nosuperuser bypassrls;
  create schema auth;
  grant usage on schema public, auth to anon, authenticated, service_role;
  create table auth.users (
    id uuid primary key,
    email text,
    email_confirmed_at timestamptz,
    raw_user_meta_data jsonb not null default '{}'
  );
  create function auth.uid()
  returns uuid language sql stable security invoker set search_path = ''
  as $$select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid$$;
`);

ok(baseline);

test('full candidate reconstructs the complete owned catalog boundary', () => {
  assert.equal(ok(`
    select count(*)
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname in ('public', 'validation') and c.relkind = 'r'
  `), '20');
  assert.equal(ok(`
    select count(distinct conrelid)
    from pg_constraint con join pg_namespace n on n.oid = con.connamespace
    where n.nspname in ('public', 'validation') and con.contype = 'p'
  `), '20');
  assert.equal(ok(`
    select count(*)
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname in ('public', 'validation') and c.relkind = 'S'
  `), '12');
  assert.equal(ok(`
    select count(*)
    from pg_attribute a join pg_class c on c.oid = a.attrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname in ('public', 'validation') and c.relkind = 'r'
      and a.attnum > 0 and not a.attisdropped
  `), '189');
  assert.equal(ok(`
    select count(*)
    from pg_constraint con join pg_namespace n on n.oid = con.connamespace
    where n.nspname in ('public', 'validation')
  `), '104');
  assert.equal(ok(`
    select count(*)
    from pg_index x join pg_class c on c.oid = x.indrelid
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname in ('public', 'validation') and x.indisvalid and x.indisready
  `), '67');
});

test('RLS is enabled everywhere and validation remains service-internal', () => {
  assert.equal(ok(`
    select count(*)
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname in ('public', 'validation') and c.relkind = 'r'
      and c.relrowsecurity
  `), '20');
  assert.equal(ok(`
    select count(*) from pg_policies where schemaname = 'validation'
  `), '0');
  for (const role of ['anon', 'authenticated', 'service_role']) {
    assert.equal(ok(`select has_schema_privilege('${role}', 'validation', 'USAGE')`), 'f');
    assert.equal(ok(`
      select has_table_privilege(
        '${role}', 'validation.consolidated_sales', 'SELECT'
      )
    `), 'f');
  }
});

test('least privilege removes anonymous access and destructive user grants', () => {
  for (const table of ['profiles', 'user_roles', 'conversations']) {
    assert.equal(ok(`
      select has_table_privilege('anon', 'public.${table}', 'SELECT')
    `), 'f');
    assert.equal(ok(`
      select has_table_privilege('authenticated', 'public.${table}', 'TRUNCATE')
    `), 'f');
  }
  for (const sequence of [
    'internal_sales_spreadsheet_rows_id_seq',
    'manager_team_reference_id_seq',
    'sales_team_reference_id_seq',
    'user_management_audit_id_seq',
  ]) {
    assert.equal(ok(`
      select has_sequence_privilege('authenticated', 'public.${sequence}', 'UPDATE')
    `), 'f');
  }
  assert.equal(ok(`
    select has_sequence_privilege(
      'authenticated', 'public.conversation_messages_id_seq', 'USAGE'
    )
  `), 't');
});

test('security-definer entry points have explicit callers', () => {
  assert.equal(ok(`
    select has_function_privilege(
      'anon', 'public.get_validation_sales_reconciliation(date,date)', 'EXECUTE'
    )
  `), 'f');
  assert.equal(ok(`
    select has_function_privilege(
      'authenticated', 'public.get_validation_sales_reconciliation(date,date)', 'EXECUTE'
    )
  `), 'f');
  assert.equal(ok(`
    select has_function_privilege(
      'service_role', 'public.get_validation_sales_reconciliation(date,date)', 'EXECUTE'
    )
  `), 't');
  for (const role of ['anon', 'authenticated', 'service_role']) {
    assert.equal(ok(`
      select has_function_privilege(
        '${role}', 'public.sync_validation_user_access()', 'EXECUTE'
      )
    `), 'f');
  }
});

test('new identities never gain privilege from email or client metadata', () => {
  const id = '11111111-1111-4111-8111-111111111111';
  assert.equal(tx(`
    insert into auth.users(id, email, email_confirmed_at, raw_user_meta_data)
    values (
      '${id}', 'baseline-admin@example.test', null,
      '{"role":"super_admin","access_role":"cmo"}'
    );
    select p.status::text || '|' || ur.role::text || '|' || p.access_role
    from public.profiles p join public.user_roles ur on ur.user_id = p.id
    where p.id = '${id}'
  `), 'invited|viewer|team_manager');
});

test('baseline contains no persisted identity or commercial rows', () => {
  for (const relation of [
    'auth.users',
    'public.profiles',
    'public.internal_sales_spreadsheet_rows',
    'public.manager_team_reference',
    'validation.brokers',
    'validation.consolidated_sales',
  ]) {
    assert.equal(ok(`select count(*) from ${relation}`), '0');
  }
});
