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
    /\["ceo","cso","cmo"\]\.includes\(profile\.access_role\)/,
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
