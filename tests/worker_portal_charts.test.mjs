import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import vm from "node:vm";
import test from "node:test";

const source = await readFile(process.env.GRALHA_TEST_WORKER_PATH || new URL("../cloudflare/gralha-indicadores-chat-worker-v11.js", import.meta.url), "utf8");
const env = { SUPABASE_URL: "https://project.supabase.co", SUPABASE_PUBLISHABLE_KEY: "test-public", OPENAI_API_KEY: "test-ai" };
const user = content => ({ role: "user", content });
const assistant = content => ({ role: "assistant", content });
const root = user("Quais bairros foram mais vendidos em julho de 2026?");
const screenshotConversation = [root, assistant("Ranking anterior"), user("Crie um gráfico"),
  assistant("Quantidade ou VGV?"), user("Deseja Top 10 (padrão), formato PNG e 800x600, com rótulos de valor"),
  assistant("Quantidade ou VGV?"), user("Sim"), assistant("Qual métrica?"), user("ambos")];
const toolsScope = vm.createContext({ Intl, Date });
vm.runInContext(source.slice(0, source.indexOf("export default")), toolsScope);
const plain = value => JSON.parse(JSON.stringify(value));
const series = [
  { label: "Bairro A", value: 10, sales_count: 10, vgv: 1000000 },
  { label: "Bairro B", value: 3, sales_count: 3, vgv: 2000000 },
];
const chartValue = (overrides = {}) => ({ visualization: {
  type: "bar", title: "Bairros", metric: "sales_count", unit: "sales", series, footnote: "Período e escopo verificados.",
  ...overrides,
} });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function runChat(t, messages, { value = chartValue(), available = true, aiOutput, mcpStatus = 200, persist = false } = {}) {
  const calls = [], stored = [];
  const previousFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = previousFetch; });
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return json({ id: "test-user" });
    if (url.includes("/rest/v1/conversations?")) return json([{ id: "11111111-1111-4111-8111-111111111111" }]);
    if (url.includes("/rest/v1/conversation_messages")) {
      if (init?.method === "POST") { stored.push(...JSON.parse(init.body)); return json([]); }
      return json([]);
    }
    if (url.includes("/gralha-indicadores-mcp/mcp")) {
      assert.equal(init.headers.Authorization, "Bearer test-session");
      const { params } = JSON.parse(init.body);
      calls.push(params);
      if (params.name === "verificar_disponibilidade_fontes") return json({ result: { structuredContent: {
        sources: Object.fromEntries(["sales", "sales_neighborhood_detail", "vista_funnel"].map(name => [name, { available }]))
      } } });
      return json({ result: { structuredContent: value } }, mcpStatus);
    }
    if (url === "https://api.openai.com/v1/responses") {
      calls.push({ name: "openai", body: JSON.parse(init.body) });
      assert.ok(aiOutput, "This chart must use the deterministic, authorized MCP path");
      return json(aiOutput);
    }
    throw new Error(`Unexpected request ${url}`);
  };
  const worker = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}#${randomUUID()}`);
  const response = await worker.default.fetch(new Request("https://worker.test/api/chat", {
    method: "POST", headers: { Authorization: "Bearer test-session", "Content-Type": "application/json" },
    body: JSON.stringify({ messages, ...(persist ? {
      conversation_id: "11111111-1111-4111-8111-111111111111", request_id: "22222222-2222-4222-8222-222222222222",
    } : {}) }),
  }), env);
  return { payload: await response.json(), status: response.status, calls, stored };
}

test("reproduces the screenshot flow without JSON or repeated confirmations", async t => {
  const { payload, calls } = await runChat(t, screenshotConversation);
  assert.equal(payload.visualization.type, "sales_comparison");
  assert.match(payload.answer, /escalas independentes/);
  assert.doesNotMatch(payload.answer, /\{|PNG|800|prefere|confirme/);
  assert.deepEqual(calls.at(-1).arguments, { data_inicio: "2026-07-01", data_fim: "2026-07-31", agrupar_por: "bairro", criterio: "quantidade", top_n: 10 });
  assert.deepEqual(payload.visualization.series.map(row => row.label), ["Bairro A", "Bairro B"]);
});

test("preserves month and topic beyond the former ten-message cutoff", async t => {
  const messages = [...screenshotConversation.slice(0, -1), assistant("Confirme"), user("Sim"), assistant("Qual?"), user("ambos")];
  assert.ok(messages.length > 10);
  const { payload } = await runChat(t, messages);
  assert.equal(payload.visualization.type, "sales_comparison");
});

test("plain chart requests choose quantity without a configuration question", async t => {
  const { payload } = await runChat(t, [root, user("Crie um gráfico")]);
  assert.equal(payload.visualization.type, "bar");
  assert.equal(payload.visualization.metric, "sales_count");
  assert.doesNotMatch(payload.answer, /\?/);
});

test("metric switch and Top N are sent to MCP, not re-sorted from an old Top 10", async t => {
  const value = chartValue({ metric: "vgv", unit: "BRL", series: [...series].reverse().map(row => ({ ...row, value: row.vgv })) });
  const { payload, calls } = await runChat(t, [root, user("Crie um gráfico"), user("Top 5 por VGV")], { value });
  assert.equal(calls.at(-1).arguments.criterio, "vgv");
  assert.equal(calls.at(-1).arguments.top_n, 5);
  assert.equal(payload.visualization.series[0].label, "Bairro B");
});

test("new topics, negations and ordinary confirmations do not inherit a chart", () => {
  for (const content of ["Qual foi o total de vendas em agosto?", "Obrigado", "Não quero gráfico"]) {
    assert.equal(toolsScope.chartRequestContext([...screenshotConversation, user(content)]), null);
  }
  assert.equal(toolsScope.chartRequestContext([root, user("Sim")]), null);
  assert.equal(toolsScope.chartRequestContext([user("Top 5 bairros em julho de 2026")]), null);
});

test("named filters, two groups and comparisons never become an unfiltered direct query", () => {
  for (const content of ["Quais bairros a Ana vendeu em julho de 2026?", "Ranking das equipes e corretores em julho de 2026", "Compare julho de 2026 com agosto de 2026", "Bairros em julho de 2026 sem Centro", "Bairros em julho e agosto de 2026", "Bairros de 1 a 15 de julho de 2026", "Gráfico da equipe Vendas em julho de 2026", "Equipes com 5 vendas em julho de 2026"]) {
    assert.equal(toolsScope.rankingChartArguments(toolsScope.chartRequestContext([user(content), user("Crie um gráfico")])), null);
  }
});

test("unavailable source and authorization rejection cannot render a graph", async t => {
  const result = await runChat(t, screenshotConversation, { available: false });
  assert.equal(result.payload.visualization, null);
  assert.equal(result.calls.length, 1);
});

test("MCP authentication failure is propagated as 401", async t => {
  const result = await runChat(t, screenshotConversation, { mcpStatus: 401 });
  assert.equal(result.status, 401);
  assert.equal(result.payload.visualization, undefined);
});

test("incomplete or unsupported chart contracts do not become fabricated zero values", () => {
  for (const vgv of [null, undefined, "", " ", false, -1, Infinity]) {
    const result = toolsScope.safeVisualization({ type: "sales_comparison", series: [{ label: "A", sales_count: 1, vgv }] });
    assert.equal(result, null);
    assert.equal(toolsScope.chartFromTool(chartValue({ series: [{ label: "A", value: 1, sales_count: 1, vgv }] }), { metric: "both" }), null);
  }
  assert.equal(toolsScope.safeVisualization({ type: "grouped_bar", series }), null);
  assert.equal(toolsScope.chartFromTool(chartValue({ series: [{ label: "A", value: 1, sales_count: 1 }] }), { metric: "both" }), null);
  assert.equal(toolsScope.chartFromTool(chartValue(), { metric: "vgv" }), null);
  assert.equal(toolsScope.chartFromTool(chartValue({ metric: "vgv" }), { metric: "both" }), null);
  assert.equal(toolsScope.chartFromTool(chartValue({ series: [...series, { label: "C", sales_count: 5, vgv: 100 }] }), { metric: "both" }), null);
});

test("AI JSON is never executed or promoted into chart data", async t => {
  const result = await runChat(t, [user("Bairros da Ana em julho de 2026"), user("Crie um gráfico")], {
    aiOutput: { output_text: '{"type":"grouped_bar","series":[{"value":999999}]}' },
  });
  assert.equal(result.payload.visualization, null);
  assert.doesNotMatch(result.payload.answer, /999999|grouped_bar|\{/);
  assert.match(result.calls.at(-1).body.instructions, /Não escreva JSON/);
});

test("filtered requests may render only the fresh structured MCP visualization", async t => {
  const result = await runChat(t, [user("Bairros da Ana em julho de 2026"), user("Crie um gráfico"), user("ambos")], {
    aiOutput: { output_text: '{"type":"grouped_bar"}', output: [{ type: "mcp_call", output: JSON.stringify({ structuredContent: chartValue() }) }] },
  });
  assert.equal(result.payload.visualization.type, "sales_comparison");
  assert.doesNotMatch(result.payload.answer, /grouped_bar/);
});

test("comparison visualization is persisted intact for history re-opening", async t => {
  const { payload, status, stored } = await runChat(t, screenshotConversation, { persist: true });
  assert.equal(status, 200);
  assert.equal(stored.length, 2);
  assert.deepEqual(stored[1].visualization, payload.visualization);
  assert.equal(stored[1].user_id, "test-user");
});

class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.attributes = {}; this.style = {}; this.textContent = ""; this.disabled = false; const classes = new Set(); this.classList = { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value), toggle: (value, force) => force ? classes.add(value) : classes.delete(value) }; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.focused = true; }
}
const flatten = el => [el, ...el.children.flatMap(flatten)];

async function clientScript() {
  const worker = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  const page = await (await worker.default.fetch(new Request("https://worker.test/"), env)).text();
  return page.match(/<script>([\s\S]*?)<\/script>/)[1];
}

test("served client script parses and comparison renders two labeled SVGs, including history", async () => {
  const script = await clientScript();
  new vm.Script(script);
  const document = { createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag), createTextNode: text => Object.assign(new Element("text"), { textContent: text }) };
  const scope = vm.createContext({ document, Intl });
  vm.runInContext(script.slice(script.indexOf("function chartValue"), script.indexOf("function addMessage")), scope);
  const data = toolsScope.chartFromTool(chartValue(), { metric: "both" });
  const output = scope.renderChart(plain(data));
  const nodes = flatten(output);
  assert.equal(nodes.filter(el => el.tag === "svg").length, 2);
  assert.equal(nodes.filter(el => el.attributes.role === "img").length, 2);
  assert.ok(nodes.some(el => el.textContent.includes("R$")));
  assert.ok(nodes.some(el => el.textContent.includes("10 vendas")));
  assert.equal(scope.renderChart({ type: "grouped_bar", series }), null);
  assert.match(script, /addMessage\(row.role,row.content,row.visualization\)/);
});

async function profileHarness(response) {
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  let historyLoads = 0;
  const script = await clientScript();
  const scope = vm.createContext({ $, document: { querySelector: () => $("connection") },
    authedRequest: response, loadHistory: async () => { historyLoads++; },
    showError: (el, text) => { el.textContent = text; el.classList.toggle("hidden", !text); },
    setTimeout, clearTimeout, session: { user: { id: "test-user" } }, profile: null, portalBootId: 0, chatError: $("chat-error") });
  vm.runInContext(script.slice(script.indexOf("async function bootPortal"), script.indexOf('$("retry-profile").addEventListener')), scope);
  return { scope, $, historyLoads: () => historyLoads };
}

test("profile HTTP, network, malformed and JSON failures leave loading and still load history", async () => {
  for (const response of [async () => json({}, 503), async () => { throw new Error("network"); }, async () => json({}), async () => new Response("invalid JSON")]) {
    const harness = await profileHarness(response);
    await harness.scope.bootPortal();
    assert.equal(harness.$("profile-role").textContent, "Perfil indisponível");
    assert.equal(harness.$("retry-profile").classList.contains("hidden"), false);
    assert.equal(harness.$("manage-users").classList.contains("hidden"), true);
    assert.equal(harness.historyLoads(), 1);
  }
});

test("profile timeout is bounded and offers manual retry, never an automatic loop", async () => {
  let calls = 0, timeout;
  const harness = await profileHarness(async () => { calls++; return new Promise(() => {}); });
  harness.scope.setTimeout = (callback, ms) => { assert.equal(ms, 15000); timeout = callback; return 1; };
  harness.scope.clearTimeout = () => {};
  const pending = harness.scope.bootPortal();
  timeout();
  await pending;
  assert.equal(calls, 1);
  assert.equal(harness.$("profile-role").textContent, "Perfil indisponível");
  assert.equal(harness.$("retry-profile").disabled, false);
});

test("successful manual retry clears error and grants UI visibility only for boolean true", async () => {
  let fail = true;
  const harness = await profileHarness(async () => fail ? json({}, 503) : json({ profile: { display_name: "Teste", role_label: "Gerente", can_manage_users: "true" } }));
  await harness.scope.bootPortal();
  fail = false;
  await harness.scope.bootPortal();
  assert.equal(harness.$("profile-role").textContent, "Gerente");
  assert.equal(harness.$("profile-error").textContent, "");
  assert.equal(harness.$("manage-users").classList.contains("hidden"), true);
  assert.equal(harness.$("retry-profile").classList.contains("hidden"), true);
});

test("late profile response cannot restore a previous session after logout", async () => {
  let finish;
  const harness = await profileHarness(() => new Promise(resolve => { finish = resolve; }));
  const pending = harness.scope.bootPortal();
  harness.scope.portalBootId++;
  harness.scope.session = null;
  finish(json({ profile: { display_name: "Anterior", role_label: "CEO", can_manage_users: true } }));
  await pending;
  assert.equal(harness.$("manage-users").classList.contains("hidden"), true);
  assert.equal(harness.historyLoads(), 0);
});

test("confirmed executive profile still exposes user management", async () => {
  const harness = await profileHarness(async () => json({ profile: { email: "teste@example.test", role_label: "CEO", can_manage_users: true } }));
  await harness.scope.bootPortal();
  assert.equal(harness.$("manage-users").classList.contains("hidden"), false);
  assert.equal(harness.$("connection").textContent, "Acesso verificado");
});

test("long UTF-8 conversations keep chronological order within the request budget", async () => {
  const script = await clientScript();
  const messages = Array.from({ length: 50 }, (_, index) => user(index + ":" + "ação😀".repeat(600)));
  messages.push(user("ambos"));
  const scope = vm.createContext({ TextEncoder, messages });
  vm.runInContext(script.slice(script.indexOf("function chatRequestMessages"), script.indexOf("async function ask")), scope);
  const result = scope.chatRequestMessages();
  assert.ok(result.length <= 40);
  assert.ok(Buffer.byteLength(JSON.stringify(result)) < 45002);
  assert.equal(result.at(-1).content, "ambos");
  assert.deepEqual(plain(result), messages.slice(-result.length));
});

async function chartRenderer() {
  const script = await clientScript();
  const document = { createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag), createTextNode: text => Object.assign(new Element("text"), { textContent: text }) };
  const scope = vm.createContext({ document, Intl });
  vm.runInContext(script.slice(script.indexOf("function chartValue"), script.indexOf("function addMessage")), scope);
  return scope;
}

test("mobile bars preserve full labels, units and all ten rows without SVG text scaling", async () => {
  const renderer = await chartRenderer();
  const label = "Bairro de nome muito longo — <img src=x onerror=alert(1)>";
  const data = { type: "bar", title: "VGV", unit: "BRL", series: Array.from({ length: 12 }, (_, i) => ({ label: i ? "Bairro " + i : label, value: 14077729.69 - i })), footnote: "Julho/2026" };
  const output = renderer.renderChart(data), nodes = flatten(output);
  const mobile = nodes.find(node => node.className === "mobile-bars");
  assert.equal(mobile.tag, "ol");
  assert.equal(mobile.children.length, 10);
  assert.equal(flatten(mobile).find(node => node.className === "mobile-bar-label").textContent, "1. " + label);
  assert.match(flatten(mobile).find(node => node.className === "mobile-bar-value").textContent, /14\.077\.730/);
  assert.equal(nodes.filter(node => node.tag === "img").length, 0);
  assert.equal(nodes.filter(node => node.tag === "svg").length, 1);
});

test("mobile comparison retains independent scales and matching category order", async () => {
  const renderer = await chartRenderer();
  const output = renderer.renderChart(plain(toolsScope.chartFromTool(chartValue(), { metric: "both" })));
  const lists = flatten(output).filter(node => node.className === "mobile-bars");
  assert.equal(lists.length, 2);
  const labels = list => flatten(list).filter(node => node.className === "mobile-bar-label").map(node => node.textContent);
  const widths = list => flatten(list).filter(node => node.className?.startsWith("mobile-bar-fill")).map(node => node.style.width);
  assert.deepEqual(labels(lists[0]), labels(lists[1]));
  assert.deepEqual(widths(lists[0]), ["100%", "30%"]);
  assert.deepEqual(widths(lists[1]), ["50%", "100%"]);
});

test("mobile zero has no visible bar and all-zero series has finite geometry", async () => {
  const renderer = await chartRenderer();
  const output = renderer.renderMobileBars({ unit: "sales", series: [{ label: "Sem vendas", value: 0 }] });
  assert.equal(flatten(output).find(node => node.className?.startsWith("mobile-bar-fill")).style.width, "0%");
  assert.ok(flatten(output).some(node => node.textContent === "0 vendas"));
});

test("mobile CSS uses real-sized text and hides only the desktop bar SVG", async () => {
  assert.match(source, /\.mobile-bar-label\{[^}]*font-size:15px/);
  assert.match(source, /\.mobile-bar-value\{[^}]*font-size:15px/);
  assert.match(source, /\.mobile-bar-heading\{[^}]*flex-wrap:wrap/);
  assert.match(source, /\.chart-bar>svg\{display:none\}\.chart-bar>\.mobile-bars\{display:grid\}/);
  assert.match(source, /\.mobile-bars,\.mobile-close\{display:none\}/);
  assert.match(source, /\.composer textarea\{[^}]*font-size:16px/);
  assert.match(source, /\.header\{position:sticky;top:0/);
});

test("history toggle is inside the header, never a fixed overlay on messages", async () => {
  const worker = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  const page = await (await worker.default.fetch(new Request("https://worker.test/"), env)).text();
  const header = page.match(/<header class="header">([\s\S]*?)<\/header>/)[1];
  assert.match(header, /id="nav-toggle"/);
  assert.equal((page.match(/id="nav-toggle"/g) || []).length, 1);
  assert.doesNotMatch(header.match(/<button id="nav-toggle"[^>]+>/)[0], /position:fixed/);
  assert.match(page, /id="close-nav"/);
  assert.match(page, /placeholder="Digite sua pergunta…"/);
});

test("mobile drawer closes accessibly and resizing restores desktop navigation", async () => {
  const script = await clientScript(), elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const mobileNavigation = { matches: true };
  const scope = vm.createContext({ $, mobileNavigation });
  vm.runInContext(script.slice(script.indexOf("function setNavOpen"), script.indexOf('mobileNavigation.addEventListener')), scope);
  scope.setNavOpen(false);
  assert.equal($("chat-nav").inert, true);
  scope.setNavOpen(true);
  assert.equal($("nav-toggle").attributes["aria-expanded"], "true");
  assert.equal($("close-nav").focused, true);
  assert.equal($("nav-backdrop").classList.contains("hidden"), false);
  scope.setNavOpen(false);
  assert.equal($("chat-nav").inert, true);
  assert.equal($("nav-backdrop").classList.contains("hidden"), true);
  assert.equal($("nav-toggle").focused, true);
  mobileNavigation.matches = false;
  scope.setNavOpen(false);
  assert.equal($("chat-nav").inert, false);
  assert.equal($("chat-nav").attributes["aria-hidden"], "false");
  assert.match(script, /event.key==="Escape"/);
});
