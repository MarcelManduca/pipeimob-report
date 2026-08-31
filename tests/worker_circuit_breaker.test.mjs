import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workerPath = new URL(
  "../cloudflare/gralha-indicadores-chat-worker-v8.js",
  import.meta.url,
);

async function loadWorker() {
  const source = await readFile(workerPath, "utf8");
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}#${randomUUID()}`;
  return import(moduleUrl);
}

function request(question) {
  return conversation([{ role: "user", content: question }]);
}

function conversation(messages) {
  return new Request("https://worker.test/api/chat", {
    method: "POST",
    headers: {
      Authorization: "Bearer test-access-token",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ messages }),
  });
}

function funnelSnapshotResponse(currentTotal = 66, openTotal = 57) {
  return new Response(
    JSON.stringify({
      jsonrpc: "2.0",
      id: "direct",
      result: {
        structuredContent: {
          summary: {
            proposal: {
              created_deals_currently_in_proposal: currentTotal,
              created_deals_in_proposal_stage_with_open_status: openTotal,
            },
          },
        },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

const env = {
  SUPABASE_URL: "https://project.supabase.co",
  SUPABASE_PUBLISHABLE_KEY: "publishable-test-key",
  OPENAI_API_KEY: "openai-test-key",
};

function availabilityResponse(available) {
  return new Response(
    JSON.stringify({
      jsonrpc: "2.0",
      id: "health",
      result: {
        structuredContent: {
          checked_at: "2026-08-31T00:00:00Z",
          sources: {
            sales: {
              verified: true,
              available,
              retry_after_seconds: available ? 0 : 42,
            },
            sales_neighborhood_detail: {
              verified: true,
              available: true,
              retry_after_seconds: 0,
            },
            vista_funnel: {
              verified: true,
              available: true,
              retry_after_seconds: 0,
            },
          },
        },
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

test("does not call OpenAI when the required source circuit is open", async () => {
  const originalFetch = globalThis.fetch;
  let openAiCalls = 0;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return new Response("{}", { status: 200 });
    if (url.includes("/functions/v1/gralha-indicadores-mcp/mcp")) {
      const body = JSON.parse(init.body);
      assert.equal(body.params.name, "verificar_disponibilidade_fontes");
      return availabilityResponse(false);
    }
    if (url === "https://api.openai.com/v1/responses") {
      openAiCalls += 1;
      return new Response(JSON.stringify({ output_text: "não deveria executar" }));
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  try {
    const worker = await loadWorker();
    const response = await worker.default.fetch(
      request("Compare o desempenho das equipes no período atual."),
      env,
    );
    const payload = await response.json();

    assert.equal(response.status, 200);
    assert.equal(openAiCalls, 0);
    assert.match(payload.answer, /antes do processamento generativo/i);
    assert.match(payload.answer, /42 segundos/i);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("calls OpenAI only after the required source is verified", async () => {
  const originalFetch = globalThis.fetch;
  let openAiCalls = 0;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return new Response("{}", { status: 200 });
    if (url.includes("/functions/v1/gralha-indicadores-mcp/mcp")) {
      return availabilityResponse(true);
    }
    if (url === "https://api.openai.com/v1/responses") {
      openAiCalls += 1;
      return new Response(
        JSON.stringify({ output_text: "Análise confirmada pelas fontes." }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  try {
    const worker = await loadWorker();
    const response = await worker.default.fetch(
      request("Compare o desempenho das equipes no período atual."),
      env,
    );
    const payload = await response.json();

    assert.equal(response.status, 200);
    assert.equal(openAiCalls, 1);
    assert.equal(payload.answer, "Análise confirmada pelas fontes.");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("uses the latest month and treats current Proposal stage as a snapshot", async () => {
  const originalFetch = globalThis.fetch;
  let openAiCalls = 0;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return new Response("{}", { status: 200 });
    if (url.includes("/functions/v1/gralha-indicadores-mcp/mcp")) {
      const body = JSON.parse(init.body);
      assert.equal(body.params.name, "consultar_funil_vista");
      assert.equal(body.params.arguments.data_inicio, "2026-08-01");
      assert.equal(body.params.arguments.data_fim, "2026-08-31");
      return funnelSnapshotResponse();
    }
    if (url === "https://api.openai.com/v1/responses") {
      openAiCalls += 1;
      throw new Error("OpenAI should not be called for a direct snapshot");
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  try {
    const worker = await loadWorker();
    const response = await worker.default.fetch(
      conversation([
        { role: "user", content: "Qual foi a quantidade de vendas em julho de 2026?" },
        { role: "assistant", content: "Foram 35 vendas em julho de 2026." },
        {
          role: "user",
          content: "Quantos negócios criados em agosto estão atualmente na etapa Proposta?",
        },
      ]),
      env,
    );
    const payload = await response.json();

    assert.equal(response.status, 200);
    assert.equal(openAiCalls, 0);
    assert.match(payload.answer, /66 negócios criados em agosto de 2026/i);
    assert.match(payload.answer, /57 estão com status “Em aberto”/i);
    assert.doesNotMatch(payload.answer, /propostas geradas/i);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("answers an open-status follow-up from the Proposal snapshot without OpenAI", async () => {
  const originalFetch = globalThis.fetch;
  let openAiCalls = 0;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.endsWith("/auth/v1/user")) return new Response("{}", { status: 200 });
    if (url.includes("/functions/v1/gralha-indicadores-mcp/mcp")) {
      const body = JSON.parse(init.body);
      assert.equal(body.params.name, "consultar_funil_vista");
      assert.equal(body.params.arguments.data_inicio, "2026-08-01");
      return funnelSnapshotResponse();
    }
    if (url === "https://api.openai.com/v1/responses") {
      openAiCalls += 1;
      throw new Error("OpenAI should not be called for a direct follow-up");
    }
    throw new Error(`unexpected fetch: ${url}`);
  };

  try {
    const worker = await loadWorker();
    const response = await worker.default.fetch(
      conversation([
        { role: "user", content: "Quantos negócios criados em agosto de 2026 estão atualmente na etapa Proposta?" },
        { role: "assistant", content: "Há 66 negócios atualmente na etapa Proposta." },
        { role: "user", content: "Quantos desses negócios estão com status Em aberto?" },
      ]),
      env,
    );
    const payload = await response.json();

    assert.equal(response.status, 200);
    assert.equal(openAiCalls, 0);
    assert.equal(
      payload.answer,
      "Desses negócios, 57 estão com status “Em aberto” na etapa Proposta.",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
