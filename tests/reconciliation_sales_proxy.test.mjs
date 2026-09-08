import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import worker from "../cloudflare/gralha-indicadores-chat-worker-v12.js";

const workerSource = await readFile(
  new URL("../cloudflare/gralha-indicadores-chat-worker-v12.js", import.meta.url),
  "utf8",
);

const MOCK_ENV = {
  SUPABASE_URL: "https://kmysinxpdkeszrtdyhid.supabase.co",
  SUPABASE_PUBLISHABLE_KEY: "mock-pub-key",
  OPENAI_API_KEY: "mock-openai-key",
  RECONCILIATION_BACKEND_URL: "https://pipeimob-report.onrender.com",
};

function createMockFetch(handlers) {
  return async (input, init = {}) => {
    const urlStr = typeof input === "string" ? input : input.url || input.href || "";
    const method = init.method || "GET";
    const headers = new Headers(init.headers || {});

    for (const handler of handlers) {
      if (handler.matches(urlStr, method, headers)) {
        return handler.handle(urlStr, method, headers, init);
      }
    }
    return new Response(JSON.stringify({ error: "unhandled_mock_url", url: urlStr }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  };
}

// ---------------------------------------------------------------------------
// 1. Static Source Code Audits
// ---------------------------------------------------------------------------

test("Worker exports /api/reconciliation/sales route with GET method check", () => {
  assert.match(
    workerSource,
    /if \(url\.pathname === "\/api\/reconciliation\/sales"\) \{\s*if \(request\.method !== "GET"\) \{\s*return json\(\{ error: "Método não permitido\." \}, 405\);/s,
  );
  assert.match(
    workerSource,
    /async function reconciliationSalesApi\(request, env, url\)/,
  );
});

test("Ensures tokens and internal keys are never exposed in responses or logs", () => {
  assert.doesNotMatch(workerSource, /console\.log\([^)]*token/i);
  assert.doesNotMatch(workerSource, /console\.log\([^)]*Authorization/i);
  assert.doesNotMatch(workerSource, /console\.log\([^)]*SUPABASE_SERVICE_ROLE_KEY/i);
});

// ---------------------------------------------------------------------------
// 2. Behavioral Tests: Happy Paths for Executive Roles (CMO, CEO, CSO)
// ---------------------------------------------------------------------------

test("Behavioral: CMO active session forwards valid request to Render and returns 200", async () => {
  const originalFetch = globalThis.fetch;
  let forwardedUrl = null;
  let forwardedAuth = null;

  try {
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo-1", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/api/reconciliation/sales"),
        handle: (url, method, headers) => {
          forwardedUrl = url;
          forwardedAuth = headers.get("Authorization");
          return new Response(
            JSON.stringify({
              contract_version: "1.1",
              summary: { official_sales: 10, matched: 10, official_vgv: "1000000.00" },
              items: [{ status: "CONCILIADO", property_code: "TEST-PROP-001" }],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        },
      },
    ]);

    const req = new Request(
      "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31&date_tolerance_days=7&refresh=true",
      {
        method: "GET",
        headers: { Authorization: "Bearer test-jwt-cmo-token" },
      },
    );

    const res = await worker.fetch(req, MOCK_ENV);
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("Content-Type"), "application/json; charset=UTF-8");

    const body = await res.json();
    assert.equal(body.contract_version, "1.1");
    assert.equal(body.summary.official_sales, 10);

    // Confirm URL, parameters, and token forwarding
    assert.ok(forwardedUrl.includes("https://pipeimob-report.onrender.com/api/reconciliation/sales"));
    assert.ok(forwardedUrl.includes("data_inicio_ccv=2026-08-01"));
    assert.ok(forwardedUrl.includes("data_fim_ccv=2026-08-31"));
    assert.ok(forwardedUrl.includes("date_tolerance_days=7"));
    assert.ok(forwardedUrl.includes("refresh=true"));
    assert.equal(forwardedAuth, "Bearer test-jwt-cmo-token");

    // Token must NOT leak into response body or headers
    assert.doesNotMatch(JSON.stringify(body), /test-jwt-cmo-token/);
    assert.doesNotMatch(JSON.stringify([...res.headers.entries()]), /test-jwt-cmo-token/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Behavioral: CEO and CSO roles are granted global access (200 OK)", async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const role of ["ceo", "cso"]) {
      globalThis.fetch = createMockFetch([
        {
          matches: (url) => url.includes("/auth/v1/user"),
          handle: () =>
            new Response(JSON.stringify({ id: `user-${role}`, email: `${role}@gralha.com.br` }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
        },
        {
          matches: (url) => url.includes("/rest/v1/profiles"),
          handle: () =>
            new Response(JSON.stringify([{ access_role: role, status: "active" }]), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
        },
        {
          matches: (url) => url.includes("/api/reconciliation/sales"),
          handle: () =>
            new Response(JSON.stringify({ summary: { official_sales: 4 } }), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
        },
      ]);

      const req = new Request(
        "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31",
        { method: "GET", headers: { Authorization: "Bearer test-token" } },
      );

      const res = await worker.fetch(req, MOCK_ENV);
      assert.equal(res.status, 200, `Expected 200 for ${role}`);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ---------------------------------------------------------------------------
// 3. Behavioral Tests: Authentication and RBAC Rejections (401, 403)
// ---------------------------------------------------------------------------

test("Behavioral: Missing or empty Bearer token returns 401 Unauthorized", async () => {
  const req1 = new Request(
    "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31",
    { method: "GET" },
  );
  const res1 = await worker.fetch(req1, MOCK_ENV);
  assert.equal(res1.status, 401);
  const data1 = await res1.json();
  assert.equal(data1.error, "Sua sessão expirou. Entre novamente.");

  const req2 = new Request(
    "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31",
    { method: "GET", headers: { Authorization: "Bearer " } },
  );
  const res2 = await worker.fetch(req2, MOCK_ENV);
  assert.equal(res2.status, 401);
});

test("Behavioral: Non-executive role (team_manager / store_director) returns 403 Forbidden", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-mgr", email: "gerente@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "team_manager", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
    ]);

    const req = new Request(
      "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31",
      { method: "GET", headers: { Authorization: "Bearer test-mgr-token" } },
    );

    const res = await worker.fetch(req, MOCK_ENV);
    assert.equal(res.status, 403);
    const data = await res.json();
    assert.equal(data.error, "Acesso exclusivo para cargos executivos (CEO, CSO e CMO).");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Behavioral: Disabled executive profile returns 403 Forbidden", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo-disabled", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "disabled" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/functions/v1/gralha-portal-admin/me"),
        handle: () =>
          new Response(JSON.stringify({ profile: { access_role: "cmo", status: "disabled" } }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
    ]);

    const req = new Request(
      "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31",
      { method: "GET", headers: { Authorization: "Bearer test-disabled-token" } },
    );

    const res = await worker.fetch(req, MOCK_ENV);
    assert.equal(res.status, 403);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ---------------------------------------------------------------------------
// 4. Behavioral Tests: Method and Parameter Validations (405, 400)
// ---------------------------------------------------------------------------

test("Behavioral: Non-GET methods return 405 Method Not Allowed", async () => {
  for (const method of ["POST", "PUT", "DELETE", "PATCH"]) {
    const req = new Request(
      "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales",
      { method, headers: { Authorization: "Bearer token" } },
    );
    const res = await worker.fetch(req, MOCK_ENV);
    assert.equal(res.status, 405, `Expected 405 for method ${method}`);
    const data = await res.json();
    assert.equal(data.error, "Método não permitido.");
  }
});

test("Behavioral: Rejects nonexistent calendar dates (e.g. 2026-02-31) with 400", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
    ]);

    const testCases = [
      "?data_inicio_ccv=2026-02-31&data_fim_ccv=2026-08-31",
      "?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-11-31",
      "?data_inicio_ccv=2026-04-31&data_fim_ccv=2026-08-31",
      "?data_inicio_ccv=2026-08-31&data_fim_ccv=2026-08-01", // start > end
      "?data_inicio_ccv=invalid-date&data_fim_ccv=2026-08-31",
    ];

    for (const qs of testCases) {
      const req = new Request(
        `https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales${qs}`,
        { method: "GET", headers: { Authorization: "Bearer test-token" } },
      );
      const res = await worker.fetch(req, MOCK_ENV);
      assert.equal(res.status, 400, `Expected 400 for ${qs}`);
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Behavioral: Rejects unknown query parameters with 400 Bad Request", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
    ]);

    const req = new Request(
      "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31&malicious_param=true",
      { method: "GET", headers: { Authorization: "Bearer test-token" } },
    );
    const res = await worker.fetch(req, MOCK_ENV);
    assert.equal(res.status, 400);
    const data = await res.json();
    assert.ok(data.error.includes("Parâmetro não permitido"));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ---------------------------------------------------------------------------
// 5. Behavioral Tests: Upstream Error and Timeout Handling
// ---------------------------------------------------------------------------

test("Behavioral: Upstream errors (401, 422, 500, non-JSON, timeout) are safely handled", async () => {
  const originalFetch = globalThis.fetch;
  try {
    // 5a. Upstream returns 401
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/api/reconciliation/sales"),
        handle: () =>
          new Response(JSON.stringify({ detail: "Authentication required." }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          }),
      },
    ]);

    const req401 = new Request(
      "https://gralha-indicadores-chat.marcelmanduca-b05.workers.dev/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31",
      { method: "GET", headers: { Authorization: "Bearer token" } },
    );
    const res401 = await worker.fetch(req401, MOCK_ENV);
    assert.equal(res401.status, 401);
    const data401 = await res401.json();
    assert.equal(data401.error, "Sua sessão expirou. Entre novamente.");

    // 5b. Upstream returns 500
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/api/reconciliation/sales"),
        handle: () =>
          new Response(JSON.stringify({ error: "internal_error" }), {
            status: 500,
            headers: { "Content-Type": "application/json" },
          }),
      },
    ]);

    const res500 = await worker.fetch(req401, MOCK_ENV);
    assert.equal(res500.status, 500);

    // 5c. Upstream returns non-JSON body (e.g. 502 Bad Gateway HTML)
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/api/reconciliation/sales"),
        handle: () =>
          new Response("<html><body>502 Bad Gateway</body></html>", {
            status: 502,
            headers: { "Content-Type": "text/html" },
          }),
      },
    ]);

    const resNonJson = await worker.fetch(req401, MOCK_ENV);
    assert.equal(resNonJson.status, 502);
    const dataNonJson = await resNonJson.json();
    assert.equal(dataNonJson.error, "Serviço de reconciliação retornou uma resposta não-JSON ou inválida.");

    // 5d. Upstream times out / throws network error
    globalThis.fetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(JSON.stringify({ id: "user-cmo", email: "cmo@gralha.com.br" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(JSON.stringify([{ access_role: "cmo", status: "active" }]), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      },
      {
        matches: (url) => url.includes("/api/reconciliation/sales"),
        handle: () => {
          throw new Error("Connection timeout");
        },
      },
    ]);

    const resTimeout = await worker.fetch(req401, MOCK_ENV);
    assert.equal(resTimeout.status, 503);
    const dataTimeout = await resTimeout.json();
    assert.equal(dataTimeout.error, "Serviço de reconciliação indisponível temporariamente.");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
