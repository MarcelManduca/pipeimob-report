import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

// No connection strings, PGHOST overrides, external clients or real credentials.
// SQL is passed only to psql INSIDE the explicitly authorized CI container.
const container = process.env.GRALHA_RLS_TEST_CONTAINER || "";
if (process.env.GITHUB_ACTIONS !== "true" ||
    process.env.GRALHA_ALLOW_DISPOSABLE_RLS_TESTS !== "true" ||
    !/^[0-9a-f]{12,64}$/.test(container)) {
  throw new Error("Requires the explicitly authorized disposable GitHub Actions PostgreSQL service");
}
const inspected = spawnSync("docker", ["inspect", container], { encoding: "utf8", timeout: 10000 });
assert.equal(inspected.status, 0, "Disposable container must exist");
const metadata = JSON.parse(inspected.stdout)[0];
assert.equal(metadata.Config.Image, "postgres:17.11-bookworm");
assert.equal(Object.keys(metadata.HostConfig.PortBindings || {}).length, 0, "No exposed database ports");
assert.ok(metadata.Config.Env.includes("POSTGRES_DB=gralha_rls_ci"));

const A = "11111111-1111-4111-8111-111111111111";
const B = "22222222-2222-4222-8222-222222222222";
const MISSING = "99999999-9999-4999-8999-999999999999";
const CA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const NEW = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const roles = ["ceo", "cso", "cmo", "store_director", "team_manager"];
const tables = ["profiles", "user_team_access", "conversations", "conversation_messages"];

function sql(text) {
  return spawnSync("docker", ["exec", "-i", container, "psql", "-X", "-qAt",
    "-U", "postgres", "-d", "gralha_rls_ci", "-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate"], {
    input: text, encoding: "utf8", timeout: 15000, maxBuffer: 1024 * 1024,
  });
}
function ok(text) {
  const result = sql(text);
  assert.equal(result.status, 0, `SQL failed: ${result.stderr.trim() || result.error?.message || "unknown"}`);
  return result.stdout.trim();
}
function file(path) { return readFileSync(new URL(`../${path}`, import.meta.url), "utf8"); }
function command(text, { role = "authenticated", uid = A, setup = "" } = {}) {
  assert.ok(["anon", "authenticated", "service_role"].includes(role));
  assert.ok(uid === "" || /^[0-9a-f-]{36}$/.test(uid));
  return `begin; set local statement_timeout = '5s'; ${setup}
    set local "request.jwt.claim.sub" = '${uid}'; set local role ${role};
    ${text}; rollback;`;
}
function scalar(text, options) { return ok(command(text, options)); }
function denied(text, options) {
  const result = sql(command(text, options));
  assert.notEqual(result.status, 0, "Operation must be denied");
  assert.match(result.stderr, /42501/, "Expected insufficient_privilege / RLS rejection, not an unrelated SQL error");
}
function profile(role, status = "active") {
  assert.ok(roles.includes(role));
  assert.ok(["active", "disabled", "invited"].includes(status));
  return `update public.profiles set access_role = '${role}', status = '${status}' where id = '${A}';`;
}
function count(table, where = "true") { return `select count(*) from public.${table} where ${where}`; }
function affected(statement) { return `with affected as (${statement} returning 1) select count(*) from affected`; }

// Build from the real repository portal migrations. Only missing upstream
// prerequisites are synthetic; the policies/helper under test are not copied.
assert.equal(ok("select current_database()"), "gralha_rls_ci");
ok(file("tests/fixtures/portal_rls_bootstrap.sql"));
for (const name of [
  "20260901150000_add_portal_conversations_and_rbac.sql",
  "20260901182000_add_conversation_message_idempotency.sql",
  "20260901191000_tune_portal_policies_and_indexes.sql",
]) ok(file(`supabase/migrations/${name}`));

ok(`
  insert into public.profiles(id,status,access_role) values
    ('${A}','active','team_manager'), ('${B}','active','cmo');
  insert into public.teams(team_key,team_name) values ('synthetic_a','Synthetic A'), ('synthetic_b','Synthetic B');
  insert into public.user_team_access(user_id,team_key) values ('${A}','synthetic_a'), ('${B}','synthetic_b');
  insert into public.conversations(id,user_id,title) values ('${CA}','${A}','Synthetic A'), ('${CB}','${B}','Synthetic B');
  insert into public.conversation_messages(conversation_id,user_id,role,content) values
    ('${CA}','${A}','user','Synthetic A'), ('${CB}','${B}','user','Synthetic B');
  grant select(display_name), update(status), references(id) on public.profiles to public;
`);
const oldPolicies = ok("select policyname from pg_policies where schemaname='public' and tablename in ('profiles','user_team_access','conversations','conversation_messages') order by policyname");

// Negative controls must reproduce both weaknesses before applying the SQL.
assert.equal(scalar(count("conversations"), { setup: profile("team_manager", "disabled") }), "1");
assert.equal(ok("select has_table_privilege('anon','public.profiles','TRUNCATE')"), "t");
ok(file("docs/security/portal_history_access_proposal.sql"));

test("negative controls reproduced, actual SQL proposal applied atomically", () => {
  assert.equal(ok("select count(*) from pg_policies where schemaname='public' and policyname in ('conversations_require_active_profile','conversation_messages_require_active_profile') and permissive='RESTRICTIVE' and cmd='ALL'"), "2");
  const retained = ok("select policyname from pg_policies where schemaname='public' and tablename in ('profiles','user_team_access','conversations','conversation_messages') and policyname not in ('conversations_require_active_profile','conversation_messages_require_active_profile') order by policyname");
  assert.equal(retained, oldPolicies);
});

test("all four tables retain RLS and API roles cannot bypass it", () => {
  assert.equal(ok(`select count(*) from pg_class where oid in (${tables.map(t => `'public.${t}'::regclass`).join(",")}) and relrowsecurity`), "4");
  assert.equal(ok("select count(*) from pg_roles where rolname in ('anon','authenticated') and (rolsuper or rolbypassrls)"), "0");
  assert.equal(scalar("select current_user"), "authenticated");
});

test("profiles least privilege includes column ACLs and PUBLIC inheritance", () => {
  for (const role of ["anon", "authenticated"]) {
    for (const privilege of ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"]) {
      assert.equal(ok(`select has_table_privilege('${role}','public.profiles','${privilege}')`), role === "authenticated" && privilege === "SELECT" ? "t" : "f");
    }
    for (const privilege of ["INSERT", "UPDATE", "REFERENCES"]) {
      assert.equal(ok(`select has_any_column_privilege('${role}','public.profiles','${privilege}')`), "f");
    }
  }
  assert.equal(ok("select has_any_column_privilege('anon','public.profiles','SELECT')"), "f");
});

test("anon cannot read or write any of the four portal tables", () => {
  for (const table of tables) denied(count(table), { role: "anon" });
  denied(`insert into public.conversations(user_id,title) values ('${A}','Synthetic')`, { role: "anon" });
  denied(`update public.profiles set status='active' where id='${A}'`, { role: "anon" });
});

for (const role of roles) {
  test(`active ${role}: own history CRUD and original profile/team visibility`, () => {
    const options = { setup: profile(role) };
    assert.equal(scalar(count("conversations"), options), "1");
    assert.equal(scalar(count("conversation_messages"), options), "1");
    assert.equal(scalar(count("profiles"), options), ["ceo", "cso", "cmo"].includes(role) ? "2" : "1");
    assert.equal(scalar(count("user_team_access"), options), ["ceo", "cso", "cmo"].includes(role) ? "2" : "1");
    assert.equal(scalar(affected(`insert into public.conversations(id,user_id,title) values ('${NEW}','${A}','Synthetic new')`), options), "1");
    assert.equal(scalar(affected(`update public.conversations set title='Synthetic renamed' where id='${CA}'`), options), "1");
    assert.equal(scalar(affected(`delete from public.conversations where id='${CA}'`), options), "1");
    assert.equal(scalar(affected(`insert into public.conversation_messages(conversation_id,user_id,role,content) values ('${CA}','${A}','assistant','Synthetic reply')`), options), "1");
    assert.equal(scalar(affected(`delete from public.conversation_messages where conversation_id='${CA}'`), options), "1");
    denied(`update public.conversation_messages set content='Synthetic edit' where conversation_id='${CA}'`, options);
    denied(`update public.profiles set status='active' where id='${A}'`, options);
  });

  test(`active ${role}: no executive bypass of history ownership or parent linkage`, () => {
    const options = { setup: profile(role) };
    assert.equal(scalar(count("conversations", `id='${CB}'`), options), "0");
    assert.equal(scalar(count("conversation_messages", `conversation_id='${CB}'`), options), "0");
    assert.equal(scalar(affected(`update public.conversations set title='Denied' where id='${CB}'`), options), "0");
    assert.equal(scalar(affected(`delete from public.conversations where id='${CB}'`), options), "0");
    assert.equal(scalar(affected(`delete from public.conversation_messages where conversation_id='${CB}'`), options), "0");
    denied(`insert into public.conversations(user_id,title) values ('${B}','Denied')`, options);
    denied(`update public.conversations set user_id='${B}' where id='${CA}'`, options);
    denied(`insert into public.conversation_messages(conversation_id,user_id,role,content) values ('${CB}','${A}','user','Denied')`, options);
    denied(`insert into public.conversation_messages(conversation_id,user_id,role,content) values ('${CA}','${B}','user','Denied')`, options);
  });

  for (const status of ["disabled", "invited"]) {
    test(`${status} ${role}: unchanged identity cannot read or mutate its history`, () => {
      const options = { setup: profile(role, status) };
      assert.equal(scalar("select auth.uid()", options), A);
      assert.equal(scalar(count("conversations"), options), "0");
      assert.equal(scalar(count("conversation_messages"), options), "0");
      assert.equal(scalar(affected(`update public.conversations set title='Denied' where id='${CA}'`), options), "0");
      assert.equal(scalar(affected(`delete from public.conversations where id='${CA}'`), options), "0");
      assert.equal(scalar(affected(`delete from public.conversation_messages where conversation_id='${CA}'`), options), "0");
      denied(`insert into public.conversations(user_id,title) values ('${A}','Denied')`, options);
      denied(`insert into public.conversation_messages(conversation_id,user_id,role,content) values ('${CA}','${A}','user','Denied')`, options);
    });
  }
}

test("missing profile and missing identity fail closed", () => {
  for (const uid of [MISSING, ""]) {
    assert.equal(scalar(count("conversations"), { uid }), "0");
    assert.equal(scalar(count("conversation_messages"), { uid }), "0");
    denied(`insert into public.conversations(user_id,title) values ('${MISSING}','Denied')`, { uid });
  }
});

test("invalid and null roles fail closed even in deliberately corrupted synthetic fixtures", () => {
  // Fixture-only schema mutation rolls back after each assertion.
  for (const roleSql of ["'invalid_role'", "null"]) {
    const setup = `alter table public.profiles drop constraint profiles_access_role_valid;
      alter table public.profiles alter column access_role drop not null;
      update public.profiles set access_role=${roleSql} where id='${A}';`;
    assert.equal(scalar(count("conversations"), { setup }), "0");
    assert.equal(scalar(count("conversation_messages"), { setup }), "0");
    denied(`insert into public.conversations(user_id,title) values ('${A}','Denied')`, { setup });
  }
});

test("restrictive status guard cannot be bypassed by an additional permissive policy", () => {
  const setup = `${profile("cmo", "disabled")}
    create policy synthetic_allow_all on public.conversations for all to authenticated using (true) with check (true);
    create policy synthetic_allow_all on public.conversation_messages for all to authenticated using (true) with check (true);`;
  assert.equal(scalar(count("conversations"), { setup }), "0");
  assert.equal(scalar(count("conversation_messages"), { setup }), "0");
  denied(`insert into public.conversations(user_id,title) values ('${A}','Denied')`, { setup });
});

test("deactivation and reactivation use the current profile, not a cached claim", () => {
  assert.equal(scalar(`select count(*) from public.conversations;
    reset role; update public.profiles set status='disabled' where id='${A}'; set local role authenticated;
    select count(*) from public.conversations;
    reset role; update public.profiles set status='active' where id='${A}'; set local role authenticated;
    select count(*) from public.conversations`), "1\n0\n1");
});

test("idempotency and cascade deletion still work for an active owner", () => {
  const key = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const row = `('${CA}','${A}','assistant','Synthetic idempotent','${key}')`;
  assert.equal(scalar(`insert into public.conversation_messages(conversation_id,user_id,role,content,request_id) values ${row};
    insert into public.conversation_messages(conversation_id,user_id,role,content,request_id) values ${row} on conflict do nothing;
    select count(*) from public.conversation_messages where request_id='${key}'`), "1");
  assert.equal(scalar(`delete from public.conversations where id='${CA}'; select count(*) from public.conversation_messages where conversation_id='${CA}'`), "0");
});

test("service_role retains server-side access; no extra helper or public RPC was added", () => {
  for (const table of tables) assert.equal(scalar(count(table), { role: "service_role" }), "2");
  assert.equal(scalar(affected(`update public.profiles set status='disabled' where id='${A}'`), { role: "service_role" }), "1");
  for (const privilege of ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"]) {
    assert.equal(ok(`select has_table_privilege('service_role','public.profiles','${privilege}')`), "t");
  }
  assert.equal(ok("select has_function_privilege('authenticated','private.current_user_is_executive()','EXECUTE')"), "t");
  assert.equal(ok("select has_function_privilege('anon','private.current_user_is_executive()','EXECUTE')"), "f");
});
