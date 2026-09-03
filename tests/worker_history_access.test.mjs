import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../cloudflare/gralha-indicadores-chat-worker-v11.js", import.meta.url), "utf8");
const proposal = await readFile(new URL("../docs/security/portal_history_access_proposal.sql", import.meta.url), "utf8");
const userId = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";
const conversationId = "33333333-3333-4333-8333-333333333333";
const requestId = "44444444-4444-4444-8444-444444444444";
const roles = ["ceo", "cso", "cmo", "store_director", "team_manager"];
const env = {
  SUPABASE_URL: "https://supabase.test",
  SUPABASE_PUBLISHABLE_KEY: "synthetic-publishable-key",
  OPENAI_API_KEY: "synthetic-openai-key",
};
const routes = [
  ["GET", "/api/conversations"],
  ["POST", "/api/conversations", { title: "Teste" }],
  ["GET", `/api/conversations/${conversationId}/messages`],
  ["PATCH", `/api/conversations/${conversationId}`, { title: "Novo título" }],
  ["DELETE", `/api/conversations/${conversationId}`],
  ["POST", "/api/chat", {
    conversation_id: conversationId, request_id: requestId,
    messages: [{ role: "user", content: "Pergunta fictícia" }],
  }],
];

function request(route, token = "synthetic-session") {
  const [method, path, body] = route;
  return new Request(`https://worker.test${path}`, {
    method,
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
}

function harness(options = {}) {
  const calls = [];
  const state = { profile: { id: userId, status: "active", access_role: "team_manager" }, ...options };
  const sandbox = {
    Request, Response, Headers, URL, URLSearchParams, AbortController,
    setTimeout: options.fastTimeout ? (fn, ms) => setTimeout(fn, Math.min(ms, 10)) : setTimeout,
    clearTimeout,
    // No real network or user records: unexpected destinations are rejected.
    fetch: async (input, init = {}) => {
      const url = new URL(typeof input === "string" ? input : input.url);
      calls.push({ url, init });
      assert.equal(url.origin, env.SUPABASE_URL);
      assert.equal(init.headers.Authorization, "Bearer synthetic-session");
      assert.equal(init.headers.apikey, env.SUPABASE_PUBLISHABLE_KEY);
      if (url.pathname === "/auth/v1/user") {
        if (state.authThrows) throw new Error("synthetic-private-upstream-detail");
        if (state.authHangs) return new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(new Error("timeout")), { once: true }));
        return Response.json(state.authStatus === 401 ? {} : { id: userId, user_metadata: { status: "active", access_role: "ceo" } }, { status: state.authStatus || 200 });
      }
      if (url.pathname === "/rest/v1/profiles") {
        assert.equal(url.searchParams.get("select"), "id,status,access_role");
        assert.equal(url.searchParams.get("id"), `eq.${userId}`);
        assert.equal(url.searchParams.get("limit"), "1");
        assert.equal(init.cache, "no-store");
        if (state.profileThrows) throw new Error("synthetic-private-upstream-detail");
        if (state.profileHangs) return new Promise((_, reject) => init.signal.addEventListener("abort", () => reject(new Error("timeout")), { once: true }));
        if (state.profileStatus) return new Response("synthetic-private-upstream-detail", { status: state.profileStatus });
        if (state.malformed) return new Response("invalid-json");
        return Response.json(state.rows ?? (state.profile ? [state.profile] : []));
      }
      assert.ok(["/rest/v1/conversations", "/rest/v1/conversation_messages"].includes(url.pathname));
      if (url.pathname === "/rest/v1/conversation_messages" && url.searchParams.has("request_id")) {
        return Response.json([{ content: "Resposta fictícia salva", visualization: null }]);
      }
      if (init.method === "DELETE") return new Response(null, { status: 204 });
      return Response.json([{ id: conversationId }], { status: init.method === "POST" ? 201 : 200 });
    },
    console: { error() {} },
  };
  vm.runInNewContext(source.replace("export default {", "globalThis.worker = {"), sandbox, { filename: "history-worker-under-test.js" });
  return { worker: sandbox.worker, calls, state };
}

for (const role of roles) {
  test(`active ${role}: all history routes and cached answers remain available`, async () => {
    for (const route of routes) {
      const { worker, calls } = harness({ profile: { id: userId, status: "active", access_role: role } });
      const response = await worker.fetch(request(route), env);
      assert.equal(response.ok, true, `${route[0]} ${route[1]}: ${response.status}`);
      assert.deepEqual(calls.slice(0, 2).map(({ url }) => url.pathname), ["/auth/v1/user", "/rest/v1/profiles"]);
      assert.equal(calls.length, 3);
      const history = calls[2];
      if (route[0] === "DELETE") {
        assert.equal(response.status, 200);
        assert.equal((await response.json()).deleted, true);
      }
      if (route[1] === "/api/chat") assert.equal((await response.json()).answer, "Resposta fictícia salva");
      if (route[0] === "POST" && route[1] === "/api/conversations") {
        assert.equal(JSON.parse(history.init.body).user_id, userId);
      } else if (history.url.pathname === "/rest/v1/conversations") {
        assert.equal(history.url.searchParams.get("user_id"), `eq.${userId}`);
      }
    }
  });
  for (const status of ["disabled", "invited"]) {
    test(`${status} ${role}: valid JWT and forged metadata do not grant history access`, async () => {
      for (const route of routes) {
        const { worker, calls } = harness({ profile: { id: userId, status, access_role: role } });
        const response = await worker.fetch(request(route), env);
        assert.equal(response.status, 403);
        assert.equal(calls.length, 2, "must stop before history, MCP or OpenAI");
        assert.match((await response.json()).error, /não está ativo/);
      }
    });
  }
}

for (const [name, profile] of [
  ["missing profile", null],
  ["invalid role", { id: userId, status: "active", access_role: "super_admin" }],
  ["null role", { id: userId, status: "active", access_role: null }],
  ["unknown status", { id: userId, status: "pending", access_role: "ceo" }],
  ["another user's profile", { id: otherId, status: "active", access_role: "ceo" }],
]) {
  test(`${name}: fail closed on every history route`, async () => {
    for (const route of routes) {
      const { worker, calls } = harness({ profile });
      assert.equal((await worker.fetch(request(route), env)).status, 403);
      assert.equal(calls.length, 2);
    }
  });
}

for (const options of [
  { profileStatus: 500 }, { profileStatus: 403 }, { profileThrows: true },
  { malformed: true }, { rows: {} }, { authThrows: true },
  { profileHangs: true, fastTimeout: true }, { authHangs: true, fastTimeout: true },
]) {
  test(`upstream failure is sanitized and denied: ${JSON.stringify(options)}`, async () => {
    const { worker, calls } = harness(options);
    const response = await worker.fetch(request(routes[5]), env);
    assert.equal(response.status, 503);
    assert.ok(calls.every(({ url }) => ["/auth/v1/user", "/rest/v1/profiles"].includes(url.pathname)));
    assert.doesNotMatch(await response.text(), /synthetic|11111111|upstream-detail/);
  });
}

test("missing and invalid sessions return 401 without history access", async () => {
  for (const options of [{ authStatus: 401 }, { profileStatus: 401 }]) {
    const { worker, calls } = harness(options);
    assert.equal((await worker.fetch(request(routes[0]), env)).status, 401);
    assert.ok(calls.length <= 2);
  }
  const { worker, calls } = harness();
  assert.equal((await worker.fetch(request(routes[0], ""), env)).status, 401);
  assert.equal(calls.length, 0);
});

test("deactivation is rechecked on the next request, including cached answers", async () => {
  const { worker, calls, state } = harness();
  assert.equal((await worker.fetch(request(routes[5]), env)).status, 200);
  state.profile.status = "disabled";
  assert.equal((await worker.fetch(request(routes[5]), env)).status, 403);
  assert.equal(calls.filter(({ url }) => url.pathname === "/rest/v1/profiles").length, 2);
  assert.equal(calls.filter(({ url }) => url.pathname === "/rest/v1/conversation_messages").length, 1);
});

test("SQL proposal restricts profiles grants without changing service_role or existing ownership policies", () => {
  const sql = proposal.replace(/--[^\n]*/g, "");
  assert.match(sql, /revoke all privileges on table public\.profiles from public, anon, authenticated;/);
  assert.match(sql, /grant select on table public\.profiles to authenticated;/);
  assert.doesNotMatch(sql, /service_role|drop policy|security definer|auth\.jwt|user_metadata/i);
  assert.match(sql, /^\s*begin;/);
  assert.match(sql, /commit;\s*$/);
});

test("SQL proposal adds restrictive current-profile guards to USING and WITH CHECK on both tables", () => {
  for (const table of ["conversations", "conversation_messages"]) {
    const policy = proposal.match(new RegExp(`create policy ${table}_require_active_profile\\s+on public\\.${table}([\\s\\S]*?);`))?.[1];
    assert.ok(policy);
    assert.match(policy, /as restrictive\s+for all\s+to authenticated/);
    assert.match(policy, /using \(/);
    assert.match(policy, /with check \(/);
    assert.equal(policy.match(/user_id = \(select auth.uid\(\)\)/g)?.length, 2);
    assert.equal(policy.match(/profile.id = \(select auth.uid\(\)\)/g)?.length, 2);
    assert.equal(policy.match(/profile.status::text = 'active'/g)?.length, 2);
    for (const role of roles) assert.equal(policy.match(new RegExp(`'${role}'`, "g"))?.length, 2);
  }
});
