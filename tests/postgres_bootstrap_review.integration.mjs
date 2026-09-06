import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const container = process.env.GRALHA_BOOTSTRAP_TEST_CONTAINER || '';
if (process.env.GITHUB_ACTIONS !== 'true' ||
    process.env.GRALHA_ALLOW_DISPOSABLE_BOOTSTRAP_TESTS !== 'true' ||
    !/^[0-9a-f]{12,64}$/.test(container)) {
  throw new Error('Requires the explicitly enabled disposable GitHub Actions service');
}
const inspect = spawnSync('docker', ['inspect', container], {encoding:'utf8',timeout:10000});
assert.equal(inspect.status,0);
const metadata = JSON.parse(inspect.stdout)[0];
assert.equal(metadata.Config.Image,'postgres:17.11-bookworm');
assert.equal(Object.keys(metadata.HostConfig.PortBindings || {}).length,0);
assert.ok(metadata.Config.Env.includes('POSTGRES_DB=gralha_bootstrap_ci'));
const file = path => readFileSync(new URL(`../${path}`,import.meta.url),'utf8');
const md5 = text => createHash('md5').update(text).digest('hex');
function sql(text) {
  return spawnSync('docker',['exec','-i',container,'psql','-X','-qAt',
    '-h','/var/run/postgresql','-U','postgres','-d','gralha_bootstrap_ci',
    '-v','ON_ERROR_STOP=1','-v','VERBOSITY=sqlstate'],
    {input:text,encoding:'utf8',timeout:15000,maxBuffer:1024*1024});
}
function ok(text) {
  const result=sql(text);
  assert.equal(result.status,0,`SQL failure: ${result.stderr.trim() || result.error?.message}`);
  return result.stdout.trim();
}
function tx(text) {return ok(`begin;set local statement_timeout='5s';${text};rollback;`);}
const A='11111111-1111-4111-8111-111111111111';
const B='22222222-2222-4222-8222-222222222222';
const C='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const D='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const insert=(id,email,confirmed=true)=>`insert into auth.users(id,email,email_confirmed_at)
  values('${id}','${email}',${confirmed ? 'now()' : 'null'});`;
const state=`select p.status::text||'|'||r.role::text from public.profiles p
  join public.user_roles r on r.user_id=p.id where p.id='${A}'`;
const archive=file('docs/migrations/history-review/20260821164016_validation_rbac_bootstrap.sql.txt');
assert.equal((archive.match(/\[REDACTED_EMAIL\]/g)||[]).length,5);
assert.doesNotMatch(archive,/[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i);
// This derivative uses a reserved example.test identity, NOT the hidden identity.
// Never execute the archival placeholder or claim this is an exact historical replay.
const historical=archive.replaceAll('[REDACTED_EMAIL]','bootstrap-admin@example.test');
const schemaEnd=archive.indexOf('CREATE OR REPLACE FUNCTION public.sync_validation_user_access()');
const helpersStart=archive.indexOf('CREATE OR REPLACE FUNCTION public.has_role(');
assert.ok(schemaEnd>0 && helpersStart>schemaEnd);
const candidate=file('tests/fixtures/bootstrap_candidate.sql');
assert.doesNotMatch(candidate,/\[REDACTED_EMAIL\]|@[a-z]|raw_user_meta_data/);
const freshCandidate=archive.slice(0,schemaEnd)+candidate+archive.slice(helpersStart);
const hardening=file('docs/migrations/history-review/20260821164200_validation_rbac_security_hardening.sql.txt');
const portalNames=[
  ['20260901150000_add_portal_conversations_and_rbac.sql','7598bdbbc27bf90db9db4de06f9a9a82'],
  ['20260901182000_add_conversation_message_idempotency.sql','13f4cf1477a344334c294a91d358654c'],
  ['20260901191000_tune_portal_policies_and_indexes.sql','ec02f39bad429623542675bec8eae472'],
];
const portal=portalNames.map(([name,hash])=>{
  const source=file(`supabase/migrations/${name}`);assert.equal(md5(source),hash);return source;
}).join('\n');

assert.equal(ok('select current_database()'),'gralha_bootstrap_ci');
assert.equal(ok("select to_regclass('public.profiles') is null"),'t');
ok(`create role anon nologin nosuperuser nobypassrls;
create role authenticated nologin nosuperuser nobypassrls;
create role service_role nologin nosuperuser bypassrls;
create schema auth;
grant usage on schema public,auth to anon,authenticated,service_role;
create table auth.users(id uuid primary key,email text,email_confirmed_at timestamptz,
  raw_user_meta_data jsonb not null default '{}');
create function auth.uid() returns uuid language sql stable security invoker set search_path=''
as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;`);

test('bootstrap review in an isolated PostgreSQL service',async t=>{
  await t.test('current executable chain cannot bootstrap an empty database',()=>{
    const result=sql(`begin;${file('supabase/migrations/20260830162242_add_manager_team_reference.sql')};rollback;`);
    assert.notEqual(result.status,0);
    assert.match(result.stderr,/42P01/);
    assert.equal(ok("select to_regclass('public.manager_team_reference') is null"),'t');
  });
  await t.test('historical derivative grants super_admin to fixed synthetic email without confirmation',()=>{
    assert.equal(tx(historical+insert(A,'bootstrap-admin@example.test',false)+state),'active|super_admin');
  });
  await t.test('historical trigger overwrites a manually assigned legacy role',()=>{
    assert.equal(tx(historical+insert(A,'person@example.test')+
      `update public.user_roles set role='super_admin' where user_id='${A}';
       update auth.users set email='changed@example.test' where id='${A}';`+state),'active|viewer');
  });
  await t.test('historical trigger preserves disabled but historical backfill does not',()=>{
    const setup=historical+insert(A,'person@example.test')+
      `update public.profiles set status='disabled' where id='${A}';`;
    assert.equal(tx(setup+`update auth.users set email_confirmed_at=now() where id='${A}';`+state),'disabled|viewer');
    assert.equal(tx(setup+historical+state),'active|viewer');
  });
  await t.test('all historical negative controls leave the database empty',()=>{
    assert.equal(ok("select to_regclass('public.profiles') is null"),'t');
    assert.equal(ok('select count(*) from auth.users'),'0');
  });

  // Candidate baseline is only a test model; it is not a production migration.
  ok(freshCandidate+hardening);
  await t.test('candidate has no email exception and confirmation never grants admin',()=>{
    assert.equal(tx(insert(A,'bootstrap-admin@example.test',false)+state),'invited|viewer');
    assert.equal(tx(insert(A,'bootstrap-admin@example.test')+state),'active|viewer');
  });
  await t.test('candidate preserves explicit legacy admin across Auth updates',()=>{
    assert.equal(tx(insert(A,'person@example.test')+
      `update public.user_roles set role='super_admin' where user_id='${A}';
       update auth.users set email='changed@example.test' where id='${A}';`+state),'active|super_admin');
  });
  await t.test('candidate backfill preserves disabled state and explicitly assigned role',()=>{
    assert.equal(tx(insert(A,'person@example.test')+
      `update public.profiles set status='disabled' where id='${A}';
       update public.user_roles set role='super_admin' where user_id='${A}';`+
      candidate+state),'disabled|super_admin');
  });
  await t.test('candidate backfill fills missing identities without creating executives',()=>{
    assert.equal(tx(`alter table auth.users disable trigger on_validation_auth_user_access;`+
      insert(A,'backfill@example.test')+candidate+state),'active|viewer');
  });
  await t.test('candidate refuses client metadata privilege escalation',()=>{
    assert.equal(tx(insert(A,'person@example.test')+
      `update auth.users set raw_user_meta_data='{"role":"super_admin","access_role":"cmo"}',
       email_confirmed_at=now() where id='${A}';`+state),'active|viewer');
  });
  await t.test('candidate empty email rejection is atomic',()=>{
    const result=sql(`begin;${insert(A,'',false)};rollback;`);
    assert.notEqual(result.status,0);assert.match(result.stderr,/P0001/);
    assert.equal(ok('select count(*) from auth.users'),'0');
  });
  await t.test('candidate trigger is not directly executable by API roles',()=>{
    assert.equal(ok("select has_function_privilege('anon','public.sync_validation_user_access()','EXECUTE')"),'f');
    assert.equal(ok("select has_function_privilege('authenticated','public.sync_validation_user_access()','EXECUTE')"),'f');
  });

  // Minimal, explicitly synthetic dependencies. No commercial mapping is tested.
  ok(`create table public.sales_team_reference(team_key text,team_name text,source_updated_through date);
      create table public.manager_team_reference(team_key text,team_name text,source_updated_through date);`);
  ok(portal);
  await t.test('three unmodified portal migrations load over the candidate schema',()=>{
    assert.equal(ok("select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relname in ('profiles','teams','user_team_access','conversations','conversation_messages','user_management_audit') and c.relrowsecurity"),'6');
    assert.equal(ok("select count(*) from pg_enum e join pg_type t on t.oid=e.enumtypid join pg_namespace n on n.oid=t.typnamespace where n.nspname='public' and t.typname='profile_status'"),'3');
    assert.equal(ok("select count(*) from pg_roles where rolname in ('anon','authenticated') and (rolsuper or rolbypassrls)"),'0');
  });
  for(const role of ['ceo','cso','cmo','store_director','team_manager']){
    await t.test(`candidate Auth updates preserve portal role ${role}`,()=>{
      assert.equal(tx(insert(A,'person@example.test',false)+
        `update public.profiles set access_role='${role}' where id='${A}';
         update auth.users set email_confirmed_at=now() where id='${A}';
         select status::text||'|'||access_role from public.profiles where id='${A}'`),`active|${role}`);
    });
  }
  await t.test('candidate Auth confirmation does not reactivate a disabled portal profile',()=>{
    assert.equal(tx(insert(A,'person@example.test',false)+
      `update public.profiles set status='disabled',access_role='cmo' where id='${A}';
       update auth.users set email_confirmed_at=now() where id='${A}';`+state),'disabled|viewer');
  });
  await t.test('own history is isolated for authenticated synthetic identities',()=>{
    assert.equal(tx(insert(A,'a@example.test')+insert(B,'b@example.test')+
      `insert into public.conversations(id,user_id,title) values
       ('${C}','${A}','Synthetic A'),('${D}','${B}','Synthetic B');
       set local "request.jwt.claim.sub"='${A}';set local role authenticated;
       select count(*) from public.conversations`),'1');
  });
  await t.test('message idempotency index rejects duplicate request/role',()=>{
    const result=sql(`begin;${insert(A,'a@example.test')}
      insert into public.conversations(id,user_id,title) values('${C}','${A}','Synthetic');
      insert into public.conversation_messages(conversation_id,user_id,role,content,request_id)
      values('${C}','${A}','user','Synthetic','${D}'),('${C}','${A}','user','Synthetic','${D}');rollback;`);
    assert.notEqual(result.status,0);assert.match(result.stderr,/23505/);
  });
  await t.test('existing disabled-history weakness remains a separate PR35 concern',()=>{
    assert.equal(tx(insert(A,'a@example.test')+
      `insert into public.conversations(id,user_id,title) values('${C}','${A}','Synthetic');
       update public.profiles set status='disabled' where id='${A}';
       set local "request.jwt.claim.sub"='${A}';set local role authenticated;
       select count(*) from public.conversations`),'1');
  });
  await t.test('candidate backfill is repeatable without altering portal role/status',()=>{
    assert.equal(tx(insert(A,'a@example.test')+
      `update public.profiles set status='disabled',access_role='cmo' where id='${A}';`+
      candidate+candidate+
      `select count(*)::text||'|'||min(status::text)||'|'||min(access_role) from public.profiles`),'1|disabled|cmo');
  });
  await t.test('the partial reconstruction contains no persisted user or business rows',()=>{
    assert.equal(ok('select count(*) from auth.users'),'0');
    assert.equal(ok('select count(*) from public.profiles'),'0');
    assert.equal(ok('select count(*) from public.conversations'),'0');
    assert.equal(ok('select count(*) from public.sales_team_reference'),'0');
  });
});
