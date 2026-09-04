import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, readdir, access } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');
const md5 = (text) => createHash('md5').update(text, 'utf8').digest('hex');
const manifest = JSON.parse(await read('docs/migrations/reconciliation-20260903.json'));
const commercial = JSON.parse(await read(
  'docs/migrations/commercial-schema-inventory-20260903.json',
));
const aligned = manifest.remote_history.filter((row) => row.previous_local_version);
const archived = manifest.remote_history.filter((row) => !row.previous_local_version);

test('snapshot is explicitly not a production execution authorization', () => {
  assert.equal(manifest.project_ref, 'kmysinxpdkeszrtdyhid');
  assert.equal(manifest.base_commit, 'e53627b265e6193b5eca69589c859ca37119bbdb');
  assert.equal(manifest.remote_history_mutated, false);
  assert.equal(manifest.ready_for_db_push, false);
  assert.equal(manifest.remote_history.length, 7);
  assert.equal(new Set(manifest.remote_history.map((row) => row.version)).size, 7);
  assert.equal(aligned.length, 3);
  assert.equal(archived.length, 4);
});

test('three aligned filenames preserve exact remote SQL bytes', async () => {
  for (const row of aligned) {
    assert.equal(row.path, `supabase/migrations/${row.version}_${row.name}.sql`);
    const sql = await read(row.path);
    assert.equal(md5(sql), row.source_md5, row.path);
    assert.equal([...sql].length, row.source_characters);
    assert.equal(row.source_statement_count, 1);
    await assert.rejects(access(new URL(
      `supabase/migrations/${row.previous_local_version}_${row.name}.sql`, root,
    )), { code: 'ENOENT' });
  }
});

test('three unrecorded portal migrations remain byte-identical', async () => {
  assert.equal(manifest.unrecorded_portal_migrations.length, 3);
  for (const row of manifest.unrecorded_portal_migrations) {
    assert.equal(md5(await read(`supabase/migrations/${row.version}_${row.name}.sql`)), row.md5);
    assert.equal(manifest.remote_history.some((remote) => remote.version === row.version), false);
  }
  assert.equal(manifest.unrecorded_portal_migrations[0].status, 'schema_observed_dml_unverified');
});

test('history review files never enter automatic migration discovery', async () => {
  const expected = [
    ...aligned.map((row) => `${row.version}_${row.name}.sql`),
    ...manifest.unrecorded_portal_migrations.map((row) => `${row.version}_${row.name}.sql`),
  ];
  const actual = await readdir(new URL('supabase/migrations/', root));
  for (const filename of expected) assert.ok(actual.includes(filename), filename);
  for (const row of archived) {
    assert.equal(row.path, `docs/migrations/history-review/${row.version}_${row.name}.sql.txt`);
    assert.equal(actual.some((filename) => filename.startsWith(`${row.version}_`)), false);
    await access(new URL(row.path, root));
  }
});

test('unredacted archival SQL matches source with at most one terminal LF added', async () => {
  for (const row of archived.filter((row) => !row.redacted)) {
    const sql = await read(row.path);
    assert.ok(md5(sql) === row.source_md5 ||
      (sql.endsWith('\n') && md5(sql.slice(0, -1)) === row.source_md5), row.path);
  }
});

test('bootstrap review copy is explicitly redacted and not represented as original', async () => {
  const rows = archived.filter((row) => row.redacted);
  assert.equal(rows.length, 1);
  const row = rows[0];
  assert.equal(row.name, 'validation_rbac_bootstrap');
  assert.equal(row.status, 'redacted_review_only');
  const sql = await read(row.path);
  assert.notEqual(md5(sql), row.source_md5);
  assert.equal((sql.match(/\[REDACTED_EMAIL\]/g) ?? []).length, 5);
});

test('review bundle contains no email literals or credential-shaped values', async () => {
  for (const row of archived) {
    const sql = await read(row.path);
    assert.doesNotMatch(sql, /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i);
    assert.doesNotMatch(sql, /eyJ[A-Za-z0-9_-]{20,}|sb_secret_|sbp_[A-Za-z0-9]{20,}/);
    assert.doesNotMatch(sql, /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i);
  }
});

test('commercial inventory is read-only evidence, never an executable baseline', () => {
  assert.equal(commercial.source.project_ref, 'kmysinxpdkeszrtdyhid');
  assert.equal(commercial.source.base_commit, manifest.base_commit);
  assert.equal(commercial.source.catalog_read_mode, 'READ ONLY');
  assert.equal(commercial.source.production_mutated, false);
  assert.equal(commercial.source.business_rows_read, false);
  assert.equal(commercial.source.personal_data_included, false);
  assert.equal(commercial.decision.ready_for_db_push, false);
  assert.equal(commercial.decision.ready_for_production, false);
  assert.equal(commercial.decision.executable_baseline_complete, false);
  assert.equal(commercial.decision.broker_team_expansion_authorized, false);
});

test('commercial inventory fixes the exact observed catalog boundary', () => {
  assert.deepEqual(commercial.summary, {
    server_version: '17.6',
    schemas: ['private', 'public', 'validation'],
    table_count: 20,
    public_table_count: 11,
    validation_table_count: 9,
    sequence_count: 12,
    column_count: 189,
    constraint_count: 104,
    index_count: 67,
    invalid_index_count: 0,
    table_without_primary_key_count: 0,
    rls_enabled_table_count: 20,
    policy_count: 20,
    publication_membership_count: 0,
  });
  assert.equal(commercial.inventory.relations.length, 32);
  assert.equal(commercial.inventory.columns.length, 189);
  assert.equal(commercial.inventory.constraints.length, 104);
  assert.equal(commercial.inventory.indexes.length, 67);
});

test('thirteen missing tables are classified without starting team expansion', () => {
  assert.deepEqual(commercial.decision.missing_from_executable_chain.public, [
    'internal_sales_spreadsheet_rows',
    'profiles',
    'sales_team_reference',
    'user_roles',
  ]);
  assert.deepEqual(commercial.decision.missing_from_executable_chain.validation, [
    'broker_system_identifiers',
    'broker_team_history',
    'brokers',
    'consolidated_sales',
    'pipeimob_sales',
    'reconciliation_events',
    'source_ingestion_runs',
    'teams',
    'vista_gains',
  ]);
});

test('sanitized inventory contains hashes instead of sensitive executable bodies', async () => {
  const snapshot = await read('docs/migrations/commercial-schema-inventory-20260903.json');
  assert.doesNotMatch(snapshot, /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i);
  assert.doesNotMatch(snapshot, /eyJ[A-Za-z0-9_-]{20,}|sb_secret_|sbp_[A-Za-z0-9]{20,}/);
  assert.equal(commercial.inventory.functions.every((fn) =>
    typeof fn.definition_md5 === 'string' && !Object.hasOwn(fn, 'definition')), true);
  assert.equal(commercial.inventory.columns.every((column) =>
    !Object.hasOwn(column, 'default_expression')), true);
  assert.equal(commercial.inventory.policies.every((policy) =>
    !Object.hasOwn(policy, 'using_expression') && !Object.hasOwn(policy, 'check_expression')), true);
  assert.equal(commercial.inventory.extension_observations.every((extension) =>
    !Object.hasOwn(extension, 'version')), true);
});

test('full baseline candidates stay outside automatic migration discovery', async () => {
  const files = [
    'tests/fixtures/identity_baseline_candidate.sql',
    'tests/fixtures/commercial_baseline_candidate.sql',
    'tests/fixtures/baseline_least_privilege_candidate.sql',
  ];
  const sources = await Promise.all(files.map(read));
  for (const [index, source] of sources.entries()) {
    assert.match(source, /TEST CANDIDATE ONLY/);
    assert.match(source, /current_database\(\) <> 'gralha_baseline_ci'/);
    assert.doesNotMatch(source, /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i);
    assert.doesNotMatch(source, /eyJ[A-Za-z0-9_-]{20,}|sb_secret_|sbp_[A-Za-z0-9]{20,}/);
    await assert.rejects(access(new URL(
      `supabase/migrations/${files[index].split('/').at(-1)}`, root,
    )), { code: 'ENOENT' });
  }
  assert.equal((sources[0].match(/create table /g) ?? []).length, 2);
  assert.equal((sources[1].match(/create table /g) ?? []).length, 11);
  assert.doesNotMatch(sources.join('\n'), /INICIAR-VALIDACAO-EQUIPES-GRALHA/);
});

test('candidate privileges explicitly remove observed broad access', async () => {
  const identity = await read('tests/fixtures/identity_baseline_candidate.sql');
  const hardening = await read('tests/fixtures/baseline_least_privilege_candidate.sql');
  assert.match(identity, /revoke all on tables from anon, authenticated/);
  assert.match(identity, /revoke execute on functions from public, anon, authenticated/);
  assert.match(hardening, /revoke all on all tables in schema public/);
  assert.match(hardening, /revoke all on all sequences in schema public/);
  assert.match(hardening, /revoke all on schema validation/);
  assert.doesNotMatch(identity, /raw_user_meta_data|resolved_role/);
});
