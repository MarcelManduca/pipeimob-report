import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";
import vm from "node:vm";
import { authorizedFunnelSummary } from "../supabase/functions/gralha-indicadores-mcp/funnel_scope.ts";
import * as references from "../supabase/functions/gralha-indicadores-mcp/team_reference.ts";

const source = await readFile(new URL("../supabase/functions/gralha-indicadores-mcp/index.ts", import.meta.url), "utf8");
// Run the real Edge handler without downloading Deno/npm modules or using secrets.
const edgeScript = stripTypeScriptTypes(source
  .replace(/^import "jsr:[^"]+";\n/m, "")
  .replace(/^import \{ createClient \} from "npm:[^"]+";\n/m, "")
  .replace(/^import \{ authorizedFunnelSummary \} from "\.\/funnel_scope.ts";\n/m, "")
  .replace(/^import \{[\s\S]*?\} from "\.\/team_reference.ts";\n/m, ""));
const workerSource = await readFile(new URL("../cloudflare/gralha-indicadores-chat-worker-v11.js", import.meta.url), "utf8");
const worker = (await import(`data:text/javascript;base64,${Buffer.from(workerSource).toString("base64")}`)).default;

function backendFixture() {
  const assignments = [
    ["Equipe A", "Proposta", "Em aberto", 2],
    ["Equipe A", "Proposta", "Perdido", 1],
    ["Equipe A", "Visita", "Em aberto", 7],
    ["Equipe B", "Proposta", "Em aberto", 3],
    ["Equipe B", "Proposta", "Perdido", 2],
    ["Equipe B", "Visita", "Em aberto", 25],
    ["Equipe C", "Proposta", "Ganho", 45],
    ["Equipe C", "Fechamento", "Em aberto", 15],
    [null, "Proposta", "Em aberto", 5],
  ].map(([team, stage, status, deals_count]) => ({team, stage, status, deals_count, responsible: null, created_date: "2026-08-10"}));
  return {
    source: "vista_negocios_listar",
    summary: {
      created_deals: 105,
      status_breakdown: [{status: "Em aberto", deals_count: 57}, {status: "Ganho", deals_count: 45}, {status: "Perdido", deals_count: 3}],
      current_stage_breakdown: [{stage: "Proposta", deals_count: 58}, {stage: "Visita", deals_count: 32}, {stage: "Fechamento", deals_count: 15}],
      stage_status_breakdown: [
        {stage: "Proposta", deals_count: 58, status_breakdown: [{status: "Ganho", deals_count: 45}, {status: "Em aberto", deals_count: 10}, {status: "Perdido", deals_count: 3}]},
        {stage: "Visita", deals_count: 32, status_breakdown: [{status: "Em aberto", deals_count: 32}]},
        {stage: "Fechamento", deals_count: 15, status_breakdown: [{status: "Em aberto", deals_count: 15}]},
      ],
      stage_assignment_breakdown: assignments,
      proposal: {
        created_deals_currently_in_proposal: 58,
        created_deals_in_proposal_stage_with_open_status: 10,
        current_proposal_stage_status_breakdown: [{status: "Ganho", deals_count: 45}, {status: "Em aberto", deals_count: 10}, {status: "Perdido", deals_count: 3}],
        assignment_breakdown: [
          {team: "Equipe A", current_stage_deals_count: 3, open_deals_count: 2},
          {team: "Equipe B", current_stage_deals_count: 5, open_deals_count: 3},
          {team: "Equipe C", current_stage_deals_count: 45, open_deals_count: 0},
          {team: null, current_stage_deals_count: 5, open_deals_count: 5},
        ],
        internal_global_metric: "UNAUTHORIZED_PROPOSAL_FIELD",
      },
      data_quality: {missing_created_at: 17, proposal_open_without_direct_team: 5},
      future_global_metric: "UNAUTHORIZED_SUMMARY_FIELD",
    },
  };
}

function harness(role, teamKeys, status = "active") {
  let handler;
  let backendCalls = 0;
  const backend = backendFixture();
  const data = {
    profiles: {access_role: role, status},
    user_team_access: teamKeys.map(team_key => ({team_key})),
    integration_failure_diagnostics: [],
    manager_team_reference: [],
    sales_team_reference: [],
  };
  const client = {
    auth: {getUser: async () => ({data: {user: {id: "synthetic-user"}}, error: null})},
    from(table) {
      assert.ok(Object.hasOwn(data, table), `Unexpected table ${table}`);
      const result = {data: data[table], error: null};
      const query = {then(resolve, reject) {return Promise.resolve(result).then(resolve, reject)}};
      for (const method of ["select", "eq", "gte", "lte", "or", "order", "limit", "maybeSingle"]) query[method] = () => query;
      return query;
    },
  };
  const environment = {
    SUPABASE_URL: "https://supabase.test",
    SUPABASE_ANON_KEY: "synthetic-key",
    MCP_PIPEIMOB_BACKEND_URL: "https://backend.test",
  };
  const sandbox = vm.createContext({
    ...references, authorizedFunnelSummary,
    URL, Request, Response, Headers, AbortSignal, structuredClone, console,
    createClient: () => client,
    Deno: {env: {get: name => environment[name]}, serve: callback => {handler = callback}},
    EdgeRuntime: {waitUntil() {throw new Error("Unexpected diagnostic write")}},
    fetch: async (input) => {
      const url = new URL(String(input));
      assert.equal(url.origin, "https://backend.test");
      assert.equal(url.pathname, "/api/vista/funnel/cohort");
      backendCalls += 1;
      return Response.json(backend);
    },
  });
  vm.runInContext(edgeScript, sandbox, {timeout: 5000});
  return {
    backend,
    get backendCalls() {return backendCalls},
    handle: request => handler(request),
    async call(args = {}) {
      const response = await handler(new Request("https://supabase.test/functions/v1/gralha-indicadores-mcp/mcp", {
        method: "POST", headers: {Authorization: "Bearer synthetic-token", "Content-Type": "application/json"},
        body: JSON.stringify({jsonrpc: "2.0", id: 1, method: "tools/call", params: {
          name: "consultar_funil_vista", arguments: {data_inicio: "2026-08-01", data_fim: "2026-08-31", ...args},
        }}),
      }));
      return {status: response.status, payload: await response.json()};
    },
  };
}

function counts(rows, key) {
  return Object.fromEntries(rows.map(row => [row[key], row.deals_count]));
}

test("manager receives only authorized totals, stages, statuses and proposal cross-tab", async () => {
  const h = harness("team_manager", ["equipe a"]);
  const {status, payload} = await h.call({agrupar_por: "nenhum", equipe: "Equipe C"});
  assert.equal(status, 200);
  assert.equal(payload.result.isError, false);
  const result = payload.result.structuredContent;
  const summary = result.summary;
  assert.equal(summary.created_deals, 10);
  assert.deepEqual(counts(summary.current_stage_breakdown, "stage"), {Visita: 7, Proposta: 3});
  assert.deepEqual(counts(summary.status_breakdown, "status"), {"Em aberto": 9, Perdido: 1});
  assert.equal(summary.proposal.created_deals_currently_in_proposal, 3);
  assert.equal(summary.proposal.created_deals_in_proposal_stage_with_open_status, 2);
  assert.deepEqual(counts(summary.proposal.current_proposal_stage_status_breakdown, "status"), {"Em aberto": 2, Perdido: 1});
  assert.deepEqual(counts(summary.stage_status_breakdown.find(row => row.stage === "Visita").status_breakdown, "status"), {"Em aberto": 7});
  assert.equal(summary.team_funnel.team_coverage.created_deals_total, 10);
  assert.equal(summary.proposal.proposals_generated_in_period, null);
  assert.deepEqual(result.visualization.groups.map(group => group.label), ["Equipe A"]);
  assert.doesNotMatch(JSON.stringify(payload.result), /Equipe [BC]|Ganho|UNAUTHORIZED_|data_quality/);
  assert.deepEqual(JSON.parse(payload.result.content[0].text), result);
  assert.equal(h.backendCalls, 1);
});

test("director combines authorized teams without including other teams or unattributed deals", async () => {
  const {payload} = await harness("store_director", [" ÉQUIPE A ", "equipe b"]).call();
  const summary = payload.result.structuredContent.summary;
  assert.equal(summary.created_deals, 40);
  assert.deepEqual(counts(summary.current_stage_breakdown, "stage"), {Visita: 32, Proposta: 8});
  assert.deepEqual(counts(summary.status_breakdown, "status"), {"Em aberto": 37, Perdido: 3});
  assert.equal(summary.proposal.created_deals_currently_in_proposal, 8);
  assert.equal(summary.proposal.created_deals_in_proposal_stage_with_open_status, 5);
  assert.equal(summary.team_funnel.selected_team, null);
  assert.doesNotMatch(JSON.stringify(payload.result), /Equipe C|Ganho|UNAUTHORIZED_|data_quality/);
});

for (const role of ["ceo", "cso", "cmo"]) {
  test(`${role} retains the company-wide summary`, async () => {
    const h = harness(role, []);
    const {payload} = await h.call();
    assert.equal(payload.result.isError, false);
    assert.equal(payload.result.structuredContent.summary.created_deals, 105);
    assert.equal(payload.result.structuredContent.summary.proposal.created_deals_currently_in_proposal, 58);
    assert.deepEqual(payload.result.structuredContent.summary.status_breakdown, h.backend.summary.status_breakdown);
    assert.deepEqual(payload.result.structuredContent.summary.data_quality, h.backend.summary.data_quality);
  });
}

test("an authorized team with no records returns an empty scoped result", async () => {
  const {payload} = await harness("team_manager", ["equipe sem negocios"]).call();
  const result = payload.result.structuredContent;
  assert.equal(result.summary.created_deals, 0);
  assert.deepEqual(result.summary.current_stage_breakdown, []);
  assert.deepEqual(result.summary.proposal.current_proposal_stage_status_breakdown, []);
  assert.deepEqual(result.visualization.groups, []);
  assert.doesNotMatch(JSON.stringify(payload.result), /Equipe [ABC]|Ganho|UNAUTHORIZED_/);
});

test("invalid manager scope or disabled profile is rejected before querying the backend", async () => {
  for (const [teams, status] of [[[], "active"], [["equipe a", "equipe b"], "active"], [["equipe a"], "disabled"]]) {
    const h = harness("team_manager", teams, status);
    const response = await h.call();
    assert.ok([401, 403].includes(response.status));
    assert.equal(h.backendCalls, 0);
  }
});

test("incompatible team data cannot reuse global totals or invent a proposal status distribution", () => {
  assert.equal(authorizedFunnelSummary({created_deals: 105}, ["equipe a"]), null);
  assert.equal(authorizedFunnelSummary({team_funnel: {team_breakdown: [{team: "Equipe A", deals_count: 10, stage_breakdown: [], status_breakdown: []}]}}, ["equipe a"]), null);
});

test("Worker text and chart use the authorized summary returned by the real MCP handler", async () => {
  const h = harness("team_manager", ["equipe a"]);
  const originalFetch = globalThis.fetch;
  let openAiCalls = 0;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return Response.json({id: "synthetic-user"});
    if (url.includes("/functions/v1/gralha-indicadores-mcp/mcp")) return h.handle(new Request(url, init));
    if (url.includes("api.openai.com")) openAiCalls += 1;
    throw new Error(`Unexpected fetch: ${url}`);
  };
  try {
    const response = await worker.fetch(new Request("https://worker.test/api/chat", {
      method: "POST", headers: {Authorization: "Bearer synthetic-token", "Content-Type": "application/json"},
      body: JSON.stringify({messages: [{role: "user", content: "Mostre o gráfico do funil comercial em agosto de 2026."}]}),
    }), {SUPABASE_URL: "https://supabase.test", SUPABASE_PUBLISHABLE_KEY: "synthetic-key", OPENAI_API_KEY: "synthetic-key"});
    const result = await response.json();
    assert.equal(response.status, 200);
    assert.match(result.answer, /10 negócios/);
    assert.doesNotMatch(result.answer, /105|100/);
    assert.equal(result.visualization.type, "funnel");
    assert.equal(result.visualization.series.reduce((sum, row) => sum + row.value, 0), 10);
    assert.doesNotMatch(JSON.stringify(result.visualization), /Equipe [BC]/);
    assert.equal(openAiCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
