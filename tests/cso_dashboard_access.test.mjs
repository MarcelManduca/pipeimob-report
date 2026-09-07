import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const worker = await readFile(
  new URL("../cloudflare/gralha-indicadores-chat-worker-v12.js", import.meta.url),
  "utf8",
);
const mcp = await readFile(
  new URL("../supabase/functions/gralha-indicadores-mcp/index.ts", import.meta.url),
  "utf8",
);

test("shows the executive dashboard only to global executive roles", () => {
  assert.match(
    worker,
    /profile\.has_global_access===true\|\|profile\.can_manage_users===true/,
  );
  assert.match(mcp, /const EXECUTIVE_ROLES = new Set\(\["ceo", "cso", "cmo"\]\)/);
  assert.match(
    mcp,
    /name === "consultar_painel_cso"[\s\S]*?!auth\.hasGlobalAccess/,
  );
});

test("does not fabricate spreadsheet-only CSO financial metrics", () => {
  assert.match(mcp, /"adjusted_vgv_business_rule"/);
  assert.match(mcp, /"cso_commission_by_role"/);
  assert.match(mcp, /"cso_received_amount"/);
  assert.match(worker, /permanecem indisponíveis até existir regra oficial/);
});

test("exposes the dashboard through an authenticated portal route", () => {
  assert.match(worker, /async function csoDashboardApi\(request\)/);
  assert.match(worker, /callMcpToolDirect\(accessToken, "consultar_painel_cso"/);
  assert.match(worker, /url\.pathname === "\/api\/cso-dashboard"/);
});

test("accepts the top-level contract returned by dashboard full", () => {
  assert.match(
    mcp,
    /Array\.isArray\(upstream\.managers\)[\s\S]*Array\.isArray\(upstream\.origins\)[\s\S]*Array\.isArray\(upstream\.timeline\)/,
  );
  assert.match(
    mcp,
    /summary: upstream\.summary,[\s\S]*managers: upstream\.managers,[\s\S]*origins: upstream\.origins,[\s\S]*timeline: upstream\.timeline/,
  );
});

test("adminApi enforces explicit timeout and returns 503 on failure", () => {
  assert.match(worker, /const ADMIN_TIMEOUT_MS = 35_000;/);
  assert.match(worker, /signal:\s*AbortSignal\.timeout\(ADMIN_TIMEOUT_MS\)/);
  assert.match(worker, /catch\s*\{\s*return json\(\{ error: "Serviço indisponível\." \}, 503\);\s*\}/);
});

test("hides CSO executive button in initial HTML until authorized", () => {
  assert.match(
    worker,
    /<button id="cso-dashboard-button" class="nav-button hidden" type="button">/,
  );
});

test("handles error states in bootPortal with cold-start retry and preserves session on 503", () => {
  assert.match(worker, /headers\.apikey\s*=\s*env\.SUPABASE_PUBLISHABLE_KEY/);
  assert.match(worker, /env\.SUPABASE_URL \? supabaseUrl\(env, "\/functions\/v1\/gralha-portal-admin"\) : ADMIN_URL/);
  assert.match(
    worker,
    /if\(!res\.ok&&res\.status===503\)\{\s*await new Promise\(r=>setTimeout\(r,1200\)\);\s*res=await authedRequest\("\/api\/admin\/me"\)\s*\}/,
  );
  assert.match(
    worker,
    /\$\("profile-role"\)\.textContent\s*=\s*res\.status===401\?"Sessão expirada":"Perfil indisponível"/,
  );
  assert.match(
    worker,
    /if\(res\.status===401\)\{save\(null\);showLogin\(\)\}/,
  );
});

test("renders monthly timeline table headers with visible contrast and sticky styling", () => {
  assert.match(
    worker,
    /<thead><tr><th>Período<\/th><th>Vendas<\/th><th>VGV<\/th><th>VGC<\/th><\/tr><\/thead>/,
  );
  assert.match(
    worker,
    /\.cso-table th\{[^}]*position:\s*sticky;[^}]*top:\s*0;[^}]*z-index:\s*2;[^}]*background:\s*#f7f8fc;[^}]*color:\s*var\(--ink\);[^}]*font-weight:\s*750;/,
  );
});

test("uses faithful broker nomenclature for VGV por corretor in CSO dashboard", () => {
  assert.match(worker, /<h2>VGV por corretor<\/h2>/);
  assert.doesNotMatch(worker, /<h2>VGV por gerente<\/h2>/);
  assert.match(worker, /csoBars\(managers,"manager","volume"\)/);
});


