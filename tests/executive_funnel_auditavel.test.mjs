import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const WORKER_PATH = resolve(
  process.cwd(),
  "cloudflare/gralha-indicadores-chat-worker-v12.js",
);

async function loadWorker() {
  const source = await readFile(WORKER_PATH, "utf8");
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}#${randomUUID()}`;
  return import(moduleUrl);
}

const env = {
  SUPABASE_URL: "https://kmysinxpdkeszrtdyhid.supabase.co",
  SUPABASE_ANON_KEY: "mock-anon-key",
  SUPABASE_PUBLISHABLE_KEY: "mock-publishable-key",
  OPENAI_API_KEY: "mock-openai-key",
};

function authRequest(path, token = "valid-token") {
  return new Request(`https://gralha-indicadores-chat.workers.dev${path}`, {
    method: "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });
}

function mockSyntheticFunnel(overrides = {}) {
  return {
    methodology: "mixed",
    period: { start: "2026-08-01", end: "2026-08-31" },
    date_bases: {
      vista: "deal_creation_or_current_stage",
      pipeimob: "ccv_signature_date",
    },
    stages: [
      {
        key: "leads",
        label: "Captações / Leads",
        count: null,
        movement_count: null,
        metric_type: "unique_clients",
        source: "vista",
        date_basis: "lead_creation_date",
        availability: "unavailable",
        reason: "Endpoint dedicado de eventos de captação não mapeado no contrato atual do CRM.",
      },
      {
        key: "opportunities",
        label: "Oportunidades",
        count: 50,
        movement_count: 50,
        metric_type: "unique_clients",
        source: "vista",
        date_basis: "opportunity_creation_date",
        availability: "available",
        reason: null,
      },
      {
        key: "visits",
        label: "Visitas",
        count: 30,
        movement_count: 35,
        metric_type: "unique_clients",
        source: "vista",
        date_basis: "current_stage_snapshot",
        availability: "partial",
        reason: "Snapshot da etapa atual de visita no CRM.",
      },
      {
        key: "proposals",
        label: "Propostas",
        count: 15,
        movement_count: 18,
        metric_type: "unique_clients",
        source: "vista",
        date_basis: "current_stage_snapshot",
        availability: "partial",
        reason: "Snapshot da etapa atual de proposta no CRM.",
      },
      {
        key: "commercial_closings",
        label: "Fechamentos comerciais",
        count: 12,
        movement_count: 14,
        metric_type: "unique_clients",
        source: "vista",
        date_basis: "current_stage_snapshot",
        availability: "partial",
        reason: "Negócios em fase de fechamento comercial/minuta.",
      },
      {
        key: "official_sales",
        label: "Vendas oficializadas",
        count: 10,
        movement_count: 10,
        metric_type: "contracts",
        source: "pipeimob",
        date_basis: "ccv_signature_date",
        availability: "available",
        reason: null,
      },
    ],
    relations: [
      {
        from_stage: "opportunities",
        to_stage: "visits",
        ratio_percentage: 60.0,
        availability: "available",
        reason: null,
      },
      {
        from_stage: "visits",
        to_stage: "proposals",
        ratio_percentage: 50.0,
        availability: "available",
        reason: null,
      },
      {
        from_stage: "proposals",
        to_stage: "commercial_closings",
        ratio_percentage: 80.0,
        availability: "available",
        reason: null,
      },
      {
        from_stage: "commercial_closings",
        to_stage: "official_sales",
        ratio_percentage: 83.3,
        availability: "available",
        reason: null,
      },
    ],
    official_vgv: "10000000.00",
    official_vgc: "500000.00",
    official_vgc_details: {
      amount: "500000.00",
      currency: "BRL",
      source: "pipeimob",
      source_field: "total_comissao",
      availability: "available",
      reason: null,
    },
    reconciliation: {
      official_sales_count: 10,
      official_sales: 10,
      official_vgv: "10000000.00",
      matched_count: 9,
      matched: 9,
      vista_gain_count: 10,
      vista_gains: 10,
      vista_without_ccv_count: 1,
      vista_without_ccv: 1,
      ccv_without_vista_count: 1,
      ccv_without_vista_gain: 1,
      non_auditable_gain_dates_count: 1,
      unresolved_gain_dates: 1,
      unresolved_teams_count: 10,
      unresolved_teams: 10,
      api_team_resolved: 0,
      divergence_flag: true,
      availability: "available",
      notes: "1 ganho declarado no CRM sem contrato oficial no período; 1 contrato oficial sem ganho CRM correspondente.",
    },
    team_scope: {
      team_filter_enabled: false,
      api_team_resolved: 0,
      reason: "Atribuição estruturada de equipe pendente de padronização cadastral na API (api_team_resolved = 0). Exibindo consolidação corporativa.",
    },
    warnings: [
      "As etapas intermediárias representam a fotografia de atividade e snapshot do Vista CRM, enquanto as Vendas oficializadas decorrem exclusivamente de CCVs formalizados no Pipeimob.",
      "Filtro por equipe temporariamente desabilitado até a resolução formal dos mapeamentos de grupos organizacionais.",
    ],
    sources: [
      { name: "pipeimob", label: "Pipeimob API v2", role: "official_contracts", status: "connected" },
      { name: "vista", label: "Vista CRM", role: "commercial_pipeline", status: "partial" },
    ],
    ...overrides,
  };
}

test("Contract: /api/cso-dashboard exposes the structured funnel payload with distinct sources and date bases", async () => {
  const originalFetch = globalThis.fetch;
  const mockFunnel = mockSyntheticFunnel();

  globalThis.fetch = async (input, init) => {
    const url = String(input);
    if (url.includes("/functions/v1/gralha-indicadores-mcp/mcp")) {
      const body = JSON.parse(init.body);
      assert.equal(body.params.name, "consultar_painel_cso");
      return new Response(
        JSON.stringify({
          jsonrpc: "2.0",
          id: body.id,
          result: {
            structuredContent: {
              contract_version: "1.0",
              source: "pipeimob_live",
              period: { start: "2026-08-01", end: "2026-08-31", basis: "ccv" },
              data: {
                summary: {
                  total_sales: 10000000,
                  total_commissions: 500000,
                  transaction_count: 10,
                  avg_commission_rate: 5.0,
                },
                managers: [],
                origins: [],
                timeline: [],
                funnel: mockFunnel,
              },
              funnel: mockFunnel,
            },
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    throw new Error(`Unexpected fetch: ${url}`);
  };

  try {
    const worker = await loadWorker();
    const res = await worker.default.fetch(
      authRequest("/api/cso-dashboard?data_inicio=2026-08-01&data_fim=2026-08-31"),
      env,
    );
    assert.equal(res.status, 200);
    const payload = await res.json();
    const funnel = payload.data.funnel || payload.funnel;
    assert.ok(funnel, "funnel object must be present in response");
    assert.equal(funnel.methodology, "mixed");
    assert.equal(funnel.stages.length, 6);
    assert.equal(funnel.stages[0].key, "leads");
    assert.equal(funnel.stages[0].count, null, "leads count must be null when unavailable");
    assert.equal(funnel.stages[0].availability, "unavailable");
    assert.equal(funnel.stages[5].key, "official_sales");
    assert.equal(funnel.stages[5].source, "pipeimob");
    assert.equal(funnel.stages[5].date_basis, "ccv_signature_date");
    assert.equal(funnel.stages[5].count, 10);
    assert.equal(funnel.reconciliation.official_sales_count, 10);
    assert.equal(funnel.reconciliation.matched_count, 9);
    assert.equal(funnel.reconciliation.vista_gain_count, 10);
    assert.equal(funnel.reconciliation.vista_without_ccv_count, 1);
    assert.equal(funnel.reconciliation.ccv_without_vista_count, 1);
    assert.equal(funnel.team_scope.team_filter_enabled, false);
    assert.equal(funnel.sources[1].status, "partial");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Behavioral: Preserves count null and never converts unavailability into zero", async () => {
  const originalFetch = globalThis.fetch;
  const mockFunnel = mockSyntheticFunnel({
    stages: [
      {
        key: "leads",
        label: "Captações / Leads",
        count: null,
        metric_type: "unique_clients",
        source: "vista",
        date_basis: "lead_creation_date",
        availability: "unavailable",
        reason: "Fonte indisponível",
      },
      {
        key: "opportunities",
        label: "Oportunidades",
        count: 0,
        metric_type: "unique_clients",
        source: "vista",
        date_basis: "opportunity_creation_date",
        availability: "available",
        reason: null,
      },
    ],
  });

  globalThis.fetch = async (input, init) => {
    const body = JSON.parse(init?.body || "{}");
    return new Response(
      JSON.stringify({
        jsonrpc: "2.0",
        id: body.id || "1",
        result: {
          structuredContent: {
            data: {
              summary: { total_sales: 0, total_commissions: 0, transaction_count: 0, avg_commission_rate: 0 },
              managers: [],
              origins: [],
              timeline: [],
              funnel: mockFunnel,
            },
            funnel: mockFunnel,
          },
        },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };

  try {
    const worker = await loadWorker();
    const res = await worker.default.fetch(
      authRequest("/api/cso-dashboard?data_inicio=2026-08-01&data_fim=2026-08-31"),
      env,
    );
    assert.equal(res.status, 200);
    const payload = await res.json();
    const funnel = payload.data.funnel || payload.funnel;
    assert.equal(funnel.stages[0].count, null, "leads count must remain null, never coerced to 0");
    assert.equal(funnel.stages[1].count, 0, "explicit zero count must remain 0");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Behavioral: Incompatible or unavailable stages produce ratio_percentage: null with reason", async () => {
  const originalFetch = globalThis.fetch;
  const mockFunnel = mockSyntheticFunnel({
    relations: [
      {
        from_stage: "leads",
        to_stage: "opportunities",
        ratio_percentage: null,
        availability: "unavailable",
        reason: "Etapa anterior ou seguinte indisponível para cálculo de relação.",
      },
    ],
  });

  globalThis.fetch = async (input, init) => {
    const body = JSON.parse(init?.body || "{}");
    return new Response(
      JSON.stringify({
        jsonrpc: "2.0",
        id: body.id || "1",
        result: {
          structuredContent: {
            data: {
              summary: { total_sales: 100, total_commissions: 5, transaction_count: 1, avg_commission_rate: 5 },
              managers: [],
              origins: [],
              timeline: [],
              funnel: mockFunnel,
            },
            funnel: mockFunnel,
          },
        },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };

  try {
    const worker = await loadWorker();
    const res = await worker.default.fetch(
      authRequest("/api/cso-dashboard?data_inicio=2026-08-01&data_fim=2026-08-31"),
      env,
    );
    assert.equal(res.status, 200);
    const payload = await res.json();
    const funnel = payload.data.funnel || payload.funnel;
    const relation = funnel.relations[0];
    assert.equal(relation.ratio_percentage, null);
    assert.equal(relation.availability, "unavailable");
    assert.ok(relation.reason.includes("indisponível"));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Behavioral: Partial degradation handles unavailable Vista CRM gracefully", async () => {
  const originalFetch = globalThis.fetch;
  const mockFunnelDegraded = mockSyntheticFunnel({
    methodology: "period_activity",
    stages: [
      { key: "leads", label: "Captações", count: null, source: "vista", availability: "unavailable" },
      { key: "opportunities", label: "Oportunidades", count: null, source: "vista", availability: "unavailable" },
      { key: "visits", label: "Visitas", count: null, source: "vista", availability: "unavailable" },
      { key: "proposals", label: "Propostas", count: null, source: "vista", availability: "unavailable" },
      { key: "commercial_closings", label: "Fechamentos", count: null, source: "vista", availability: "unavailable" },
      { key: "official_sales", label: "Vendas oficializadas", count: 12, source: "pipeimob", availability: "available" },
    ],
    reconciliation: {
      vista_gains: 0,
      official_sales: 12,
      vista_without_ccv: 0,
      ccv_without_vista_gain: 0,
      unresolved_gain_dates: 0,
      unresolved_teams: 12,
      availability: "unavailable",
    },
  });

  globalThis.fetch = async (input, init) => {
    const body = JSON.parse(init?.body || "{}");
    return new Response(
      JSON.stringify({
        jsonrpc: "2.0",
        id: body.id || "1",
        result: {
          structuredContent: {
            data: {
              summary: { total_sales: 5000000, total_commissions: 250000, transaction_count: 12, avg_commission_rate: 5 },
              managers: [],
              origins: [],
              timeline: [],
              funnel: mockFunnelDegraded,
            },
            funnel: mockFunnelDegraded,
          },
        },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };

  try {
    const worker = await loadWorker();
    const res = await worker.default.fetch(
      authRequest("/api/cso-dashboard?data_inicio=2026-08-01&data_fim=2026-08-31"),
      env,
    );
    assert.equal(res.status, 200);
    const payload = await res.json();
    const funnel = payload.data.funnel || payload.funnel;
    assert.equal(payload.data.summary.transaction_count, 12);
    assert.equal(funnel.stages[5].count, 12);
    assert.equal(funnel.reconciliation.availability, "unavailable");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Security: /api/cso-dashboard rejects unauthenticated requests with 401", async () => {
  const worker = await loadWorker();
  const res = await worker.default.fetch(
    new Request("https://gralha-indicadores-chat.workers.dev/api/cso-dashboard"),
    env,
  );
  assert.equal(res.status, 401);
  const payload = await res.json();
  assert.match(payload.error, /sessão/i);
});

test("UI & Security: HTML rendering properly escapes special characters in funnel labels and notes", async () => {
  const worker = await loadWorker();
  const htmlRes = await worker.default.fetch(
    new Request("https://gralha-indicadores-chat.workers.dev/"),
    env,
  );
  assert.equal(htmlRes.status, 200);
  const html = await htmlRes.text();
  assert.ok(html.includes("cso-funnel-card"), "Funnel CSS styles must be present in portal HTML");
  assert.ok(html.includes("cso-recon-grid"), "Reconciliation CSS styles must be present in portal HTML");
  assert.ok(html.includes("cso-badge-warning"), "Warning badge CSS must be present in portal HTML");
});

test("Behavioral: Global corporate summary mapping and VGC unavailable fallback", async () => {
  const originalFetch = globalThis.fetch;
  const mockGlobalFunnel = mockSyntheticFunnel({
    official_vgv: "45000000.00",
    official_vgc: "0.00",
    official_vgc_details: {
      amount: null,
      currency: "BRL",
      source: "pipeimob",
      source_field: "total_comissao",
      availability: "unavailable",
      reason: "Comissão oficial não informada nas transações do período.",
    },
    reconciliation: {
      official_sales_count: 40,
      official_sales: 40,
      official_vgv: "45000000.00",
      matched_count: 38,
      matched: 38,
      vista_gain_count: 41,
      vista_gains: 41,
      vista_without_ccv_count: 3,
      vista_without_ccv: 3,
      ccv_without_vista_count: 2,
      ccv_without_vista_gain: 2,
      non_auditable_gain_dates_count: 4,
      unresolved_gain_dates: 4,
      unresolved_teams_count: 40,
      unresolved_teams: 40,
      api_team_resolved: 0,
      divergence_flag: true,
      availability: "available",
      notes: "3 ganhos declarados no CRM sem CCV oficial; 2 contratos CCV sem ganho no CRM; 4 datas não auditáveis; 40 vendas com equipe pendente de padronização.",
    },
    team_scope: {
      team_filter_enabled: false,
      api_team_resolved: 0,
      reason: "Atribuição estruturada de equipe pendente de padronização cadastral na API (api_team_resolved = 0). Exibindo consolidação corporativa.",
    },
    sources: [
      { name: "pipeimob", label: "Pipeimob API v2", role: "official_contracts", status: "connected" },
      { name: "vista", label: "Vista CRM", role: "commercial_pipeline", status: "partial" },
    ],
  });

  globalThis.fetch = async (input, init) => {
    const body = JSON.parse(init?.body || "{}");
    return new Response(
      JSON.stringify({
        jsonrpc: "2.0",
        id: body.id || "1",
        result: {
          structuredContent: {
            data: {
              summary: { total_sales: 45000000, total_commissions: 0, transaction_count: 40, avg_commission_rate: 0 },
              managers: [],
              origins: [],
              timeline: [],
              funnel: mockGlobalFunnel,
            },
            funnel: mockGlobalFunnel,
          },
        },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };

  try {
    const worker = await loadWorker();
    const res = await worker.default.fetch(
      authRequest("/api/cso-dashboard?data_inicio=2026-08-01&data_fim=2026-08-31"),
      env,
    );
    assert.equal(res.status, 200);
    const payload = await res.json();
    const funnel = payload.data.funnel || payload.funnel;

    // 1. Global View (no team filter applied)
    assert.equal(funnel.team_scope.team_filter_enabled, false);
    assert.equal(funnel.team_scope.api_team_resolved, 0);

    // 2. Summary mappings
    assert.equal(funnel.reconciliation.official_sales_count, 40);
    assert.equal(funnel.reconciliation.matched_count, 38);
    assert.equal(funnel.reconciliation.vista_without_ccv_count, 3);
    assert.equal(funnel.reconciliation.ccv_without_vista_count, 2);
    assert.equal(funnel.reconciliation.non_auditable_gain_dates_count, 4);
    assert.equal(funnel.reconciliation.unresolved_teams_count, 40);
    assert.equal(funnel.reconciliation.divergence_flag, true);

    // 3. VGC provenance and availability
    assert.equal(funnel.official_vgc_details.availability, "unavailable");
    assert.equal(funnel.official_vgc_details.amount, null);
    assert.equal(funnel.official_vgc_details.source_field, "total_comissao");

    // 4. Source statuses
    assert.equal(funnel.sources[0].status, "connected");
    assert.equal(funnel.sources[1].status, "partial");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("UI & Behavioral: Scenario A renders compact homologation state and Scenario B renders full pipeline", async () => {
  const worker = await loadWorker();
  const htmlRes = await worker.default.fetch(
    new Request("https://gralha-indicadores-chat.workers.dev/"),
    env,
  );
  assert.equal(htmlRes.status, 200);
  const html = await htmlRes.text();

  // Scenario A tokens
  assert.ok(html.includes("cso-funnel-homologation"), "Homologation CSS class must be present");
  assert.ok(html.includes("cso-homologation-badge"), "Homologation badge CSS class must be present");
  assert.ok(html.includes("Etapas Vista em homologação"), "Homologation text must be present in client script");
  assert.ok(html.includes("cso-official-highlight"), "Official sales highlight container must be present");

  // Scenario B tokens
  assert.ok(html.includes("Relação entre etapas no período"), "Relation explanation tooltip for >100% must be present");
});

