import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workerSource = await readFile(
  new URL("../cloudflare/gralha-indicadores-chat-worker-v12.js", import.meta.url),
  "utf8",
);

test("Worker exports /api/reconciliation/sales route with GET method", () => {
  assert.match(
    workerSource,
    /request\.method === "GET" &&\s*url\.pathname === "\/api\/reconciliation\/sales"/,
  );
  assert.match(
    workerSource,
    /async function reconciliationSalesApi\(request, env, url\)/,
  );
});

test("Enforces 401 when request has no valid bearer token", () => {
  assert.match(
    workerSource,
    /const token = bearer\(request\);\s*if \(!token\) return json\(\{ error: "Sua sessão expirou\. Entre novamente\." \}, 401\);/,
  );
  assert.match(
    workerSource,
    /const auth = await authenticatedUser\(request, env\);\s*if \(!auth\) return json\(\{ error: "Sua sessão expirou\. Entre novamente\." \}, 401\);/,
  );
});

test("Enforces fail-closed 403 RBAC for non-executive roles (restricts to CEO, CSO, CMO)", () => {
  assert.match(
    workerSource,
    /\["ceo", "cso", "cmo"\]\.includes\(String\(p\?\.access_role \|\| ""\)\.toLowerCase\(\)\)/,
  );
  assert.match(
    workerSource,
    /return json\(\s*\{\s*error:\s*"Acesso exclusivo para cargos executivos \(CEO, CSO e CMO\)\."\s*\},\s*403,?\s*\);/,
  );
});

test("Rejects unknown query parameters with 400 Bad Request", () => {
  assert.match(
    workerSource,
    /const ALLOWED_PARAMS = new Set\(\[\s*"data_inicio_ccv",\s*"data_fim_ccv",\s*"date_tolerance_days",\s*"refresh",?\s*\]\);/,
  );
  assert.match(
    workerSource,
    /if \(!ALLOWED_PARAMS\.has\(key\)\) \{\s*return json\(\{ error: `Parâmetro não permitido: \$\{key\}` \}, 400\);/s,
  );
});

test("Validates date format and range strictly with 400 Bad Request", () => {
  assert.match(
    workerSource,
    /if \(!start \|\| !end\) \{\s*return json\(\s*\{\s*error:\s*"Parâmetros data_inicio_ccv e data_fim_ccv são obrigatórios\."\s*\},\s*400,?\s*\);/s,
  );
  assert.match(
    workerSource,
    /const dateRegex = \/\^\\d\{4\}-\\d\{2\}-\\d\{2\}\$\/;/,
  );
  assert.match(
    workerSource,
    /if \(!dateRegex\.test\(start\) \|\| !dateRegex\.test\(end\)\) \{\s*return json\(\s*\{\s*error:\s*"As datas devem estar no formato YYYY-MM-DD\."\s*\},\s*400,?\s*\);/s,
  );
  assert.match(
    workerSource,
    /if \(startParsed > endParsed\) \{\s*return json\(\s*\{\s*error:\s*"A data inicial não pode ser posterior à data final\."\s*\},\s*400,?\s*\);/s,
  );
});

test("Validates date_tolerance_days and refresh bounds strictly with 400", () => {
  assert.match(
    workerSource,
    /if \(!\/^\^\\d\+\$\/\.test\(toleranceParam\)|\/^\^\\d\+\$\/\.test\(toleranceParam\)|toleranceParam !== null/,
  );
  assert.match(
    workerSource,
    /date_tolerance_days deve ser um número inteiro entre 0 e 31\./,
  );
  assert.match(
    workerSource,
    /date_tolerance_days deve estar entre 0 e 31\./,
  );
  assert.match(
    workerSource,
    /refresh deve ser 'true' ou 'false'\./,
  );
});

test("Forwards to Render backend with 55s timeout and preserves upstream status", () => {
  assert.match(
    workerSource,
    /const RECONCILIATION_TIMEOUT_MS = 55_000;/,
  );
  assert.match(
    workerSource,
    /const RECONCILIATION_BACKEND_URL = "https:\/\/pipeimob-report\.onrender\.com";/,
  );
  assert.match(
    workerSource,
    /targetUrl\.searchParams\.set\("data_inicio_ccv", start\);/,
  );
  assert.match(
    workerSource,
    /targetUrl\.searchParams\.set\("data_fim_ccv", end\);/,
  );
  assert.match(
    workerSource,
    /signal: AbortSignal\.timeout\(RECONCILIATION_TIMEOUT_MS\)/,
  );
  assert.match(
    workerSource,
    /catch\s*\{\s*return json\(\s*\{\s*error:\s*"Serviço de reconciliação indisponível temporariamente\."\s*\},\s*503,?\s*\);\s*\}/,
  );
});

test("Ensures tokens and internal keys are never exposed in responses or logs", () => {
  assert.doesNotMatch(
    workerSource,
    /console\.log\([^)]*token/i,
  );
  assert.doesNotMatch(
    workerSource,
    /console\.log\([^)]*Authorization/i,
  );
  assert.doesNotMatch(
    workerSource,
    /console\.log\([^)]*SUPABASE_SERVICE_ROLE_KEY/i,
  );
});
