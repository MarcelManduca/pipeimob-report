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
