import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, readdir, access } from 'node:fs/promises';
import test from 'node:test';

const root = new URL('../', import.meta.url);
const read = (path) => readFile(new URL(path, root), 'utf8');
const md5 = (text) => createHash('md5').update(text, 'utf8').digest('hex');
const manifest = JSON.parse(await read('docs/migrations/reconciliation-20260903.json'));
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
