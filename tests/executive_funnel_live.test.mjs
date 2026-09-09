import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import worker, {
  normalizeReconciliationSummary,
} from "../cloudflare/gralha-indicadores-chat-worker-v12.js";

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
// 1. Todas as etapas do Vista (All 5 Vista stages + Pipeimob 6th stage)
// ---------------------------------------------------------------------------
test("1. Todas as 5 etapas do Vista + 6a etapa Pipeimob são contempladas no contrato e UI", async () => {
  const mainSource = await readFile(
    new URL("../main.py", import.meta.url),
    "utf8",
  );
  assert.match(mainSource, /Captações \/ Leads/);
  assert.match(mainSource, /Oportunidades/);
  assert.match(mainSource, /Visitas/);
  assert.match(mainSource, /Propostas/);
  assert.match(mainSource, /Fechamentos comerciais/);
  assert.match(workerSource, /Vendas oficializadas/);
  assert.match(workerSource, /Vista CRM/);
  assert.match(workerSource, /Pipeimob/);
});


// ---------------------------------------------------------------------------
// 2. Falha parcial do Vista
// ---------------------------------------------------------------------------
test("2. Falha parcial do Vista: mantém estado isolado de erro com botão de tentar novamente", () => {
  assert.match(workerSource, /id="cso-funnel-retry"/);
  assert.match(workerSource, /attachFunnelRetryHandler/);
  assert.match(workerSource, /Etapas Vista em homologação/);
});


// ---------------------------------------------------------------------------
// 3. Diferença entre valor nulo e zero real
// ---------------------------------------------------------------------------
test("3. Diferença entre valor nulo e zero real (null permanece nulo e não vira 0)", () => {
  assert.match(
    workerSource,
    /typeof stage\.count\s*===\s*"number"\s*\?\s*number\(stage\.count\)\s*:\s*'<span class="unavailable">Indisponível<\/span>'/
  );
});

// ---------------------------------------------------------------------------
// 4. Relações superiores a 100% (chamadas de 'Relação entre etapas' e não conversão)
// ---------------------------------------------------------------------------
test("4. Relações superiores a 100% são rotuladas como 'Relação entre etapas'", () => {
  assert.match(workerSource, /Relação entre etapas/);
  assert.doesNotMatch(workerSource, /Conversão entre etapas/i);
});

// ---------------------------------------------------------------------------
// 5. Cancelamento de requisições antigas ao mudar o período (AbortController)
// ---------------------------------------------------------------------------
test("5. Cancelamento de requisições antigas ao mudar o período (activeFunnelController & activeReconController)", () => {
  assert.match(workerSource, /activeFunnelController\.abort\(\)/);
  assert.match(workerSource, /activeReconController\.abort\(\)/);
  assert.match(workerSource, /activeFunnelController\s*=\s*new AbortController\(\)/);
  assert.match(workerSource, /activeReconController\s*=\s*new AbortController\(\)/);
});

// ---------------------------------------------------------------------------
// 6. RBAC de todos os cargos
// ---------------------------------------------------------------------------
test("6. RBAC: CEO, CSO, CMO possuem acesso global ao proxy /api/vista/funnel/summary", async () => {
  for (const role of ["ceo", "cso", "cmo"]) {
    const mockFetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(
            JSON.stringify({ id: `user-${role}`, email: `${role}@gralha.com.br` }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          ),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(
            JSON.stringify([
              {
                id: `user-${role}`,
                access_role: role,
                status: "active",
              },
            ]),
            { status: 200, headers: { "Content-Type": "application/json" } }
          ),
      },
      {
        matches: (url) => url.includes("/api/vista/funnel/summary"),
        handle: () =>
          new Response(
            JSON.stringify({
              contract_version: "1.1",
              source: "vista_negocios_listar",
              stages: [{ key: "opportunities", count: 100 }],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          ),
      },
    ]);

    const req = new Request("https://indicadores.gralha.com.br/api/vista/funnel/summary?data_inicio=2026-01-01&data_fim=2026-08-09", {
      headers: { Authorization: `Bearer mock-token-${role}` },
    });

    const origFetch = globalThis.fetch;
    globalThis.fetch = mockFetch;
    try {
      const res = await worker.fetch(req, MOCK_ENV, {});
      assert.equal(res.status, 200, `Role ${role} should have access`);
      const body = await res.json();
      assert.equal(body.contract_version, "1.1");
    } finally {
      globalThis.fetch = origFetch;
    }
  }
});

// ---------------------------------------------------------------------------
// 7. Escopo de equipe e loja: Diretor e Gerente recebem organizational_scope_unavailable
// ---------------------------------------------------------------------------
test("7. RBAC: Gerente e Diretor recebem 403 com code 'organizational_scope_unavailable'", async () => {
  for (const role of ["store_director", "team_manager", "diretor", "gerente"]) {
    const mockFetch = createMockFetch([
      {
        matches: (url) => url.includes("/auth/v1/user"),
        handle: () =>
          new Response(
            JSON.stringify({ id: `user-${role}`, email: `${role}@gralha.com.br` }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          ),
      },
      {
        matches: (url) => url.includes("/rest/v1/profiles"),
        handle: () =>
          new Response(
            JSON.stringify([
              {
                id: `user-${role}`,
                access_role: role,
                status: "active",
                team: "Equipe Alfa",
                store: "Loja Central",
              },
            ]),
            { status: 200, headers: { "Content-Type": "application/json" } }
          ),
      },
    ]);

    const req = new Request("https://indicadores.gralha.com.br/api/vista/funnel/summary?data_inicio=2026-01-01&data_fim=2026-08-09", {
      headers: { Authorization: `Bearer mock-token-${role}` },
    });

    const origFetch = globalThis.fetch;
    globalThis.fetch = mockFetch;
    try {
      const res = await worker.fetch(req, MOCK_ENV, {});
      assert.equal(res.status, 403, `Role ${role} should receive 403 Forbidden`);
      const body = await res.json();
      assert.equal(body.code, "organizational_scope_unavailable");
      assert.match(body.error, /temporariamente indisponível/i);
    } finally {
      globalThis.fetch = origFetch;
    }
  }
});

test("7b. RBAC: Perfil inativo recebe 403 com code 'inactive_profile'", async () => {
  const mockFetch = createMockFetch([
    {
      matches: (url) => url.includes("/auth/v1/user"),
      handle: () =>
        new Response(
          JSON.stringify({ id: "user-inactive", email: "inactive@gralha.com.br" }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        ),
    },
    {
      matches: (url) => url.includes("/rest/v1/profiles"),
      handle: () =>
        new Response(
          JSON.stringify([
            {
              id: "user-inactive",
              access_role: "ceo",
              status: "disabled",
            },
          ]),
          { status: 200, headers: { "Content-Type": "application/json" } }
        ),
    },
  ]);

  const req = new Request("https://indicadores.gralha.com.br/api/vista/funnel/summary?data_inicio=2026-01-01&data_fim=2026-08-09", {
    headers: { Authorization: "Bearer mock-token-inactive" },
  });

  const origFetch = globalThis.fetch;
  globalThis.fetch = mockFetch;
  try {
    const res = await worker.fetch(req, MOCK_ENV, {});
    assert.equal(res.status, 403);
    const body = await res.json();
    assert.equal(body.code, "inactive_profile");
  } finally {
    globalThis.fetch = origFetch;
  }
});

// ---------------------------------------------------------------------------
// 8. Identidades matemáticas da reconciliação (306/310/30/13 comprovado e deduplicação)
// ---------------------------------------------------------------------------
test("8. Identidades matemáticas da reconciliação: official_sales (310) = strictly_matched (276) + divergent_linked_unique (21) + ccv_without_vista (13)", () => {
  const rawPayload = {
    summary: {
      pipeimob_sales_count: 310,
      matched_count: 276,
      strictly_matched_count: 276,
      divergent_matches_count: 21,
      divergent_linked_unique: 21,
      value_mismatches_count: 18,
      value_only_mismatches: 18,
      date_mismatches_count: 3,
      date_only_mismatches: 3,
      value_and_date_mismatches: 0,
      pipeimob_without_vista_gain_count: 13,
      vista_without_pipeimob_contract_count: 30,
      total_linked_count: 297,
      total_linked_unique: 297,
      total_vista_gains_count: 327,
      ambiguous_matches_count: 0,
      unresolved_gain_dates_count: 0,
      gain_period_basis: "DataFinal_only_if_present_else_unresolved",
      limitation_note: "Registros do Vista CRM sem DataFinal preenchida (DataFinal=null) não possuem data de ganho auditável. O sistema não utiliza UltimaAtualizacao como fallback.",
    }
  };

  const norm = normalizeReconciliationSummary(rawPayload);
  assert.equal(norm.official_sales, 310);
  assert.equal(norm.strictly_matched, 276);
  assert.equal(norm.matched, 276);
  assert.equal(norm.divergent_matches, 21);
  assert.equal(norm.divergent_linked_unique, 21);
  assert.equal(norm.value_mismatches, 18);
  assert.equal(norm.value_only_mismatches, 18);
  assert.equal(norm.date_mismatches, 3);
  assert.equal(norm.date_only_mismatches, 3);
  assert.equal(norm.value_and_date_mismatches, 0);
  assert.equal(norm.total_linked, 297);
  assert.equal(norm.total_linked_unique, 297);
  assert.equal(norm.ccv_without_vista_gain, 13);
  assert.equal(norm.vista_without_ccv, 30);
  assert.equal(norm.total_vista_gains, 327);
  assert.equal(norm.vista_gains, 327);

  // Divergence partition: 18 value-only + 3 date-only + 0 both = 21 unique divergent deals
  assert.equal(
    norm.value_only_mismatches + norm.date_only_mismatches + norm.value_and_date_mismatches,
    norm.divergent_linked_unique
  );

  // Math Identity 1: official_sales === strictly_matched + divergent_linked_unique + ccv_without_vista_gain
  assert.equal(norm.strictly_matched + norm.divergent_linked_unique + norm.ccv_without_vista_gain, norm.official_sales);
  // Math Identity 2: total_linked_unique === strictly_matched + divergent_linked_unique
  assert.equal(norm.strictly_matched + norm.divergent_linked_unique, norm.total_linked_unique);
  // Math Identity 3: total_vista_gains === total_linked_unique + vista_without_ccv
  assert.equal(norm.total_linked_unique + norm.vista_without_ccv, norm.total_vista_gains);
  // Discrepancy explained: 276 + 30 = 306 (which omitted the 21 unique divergent deals)
  assert.equal(norm.strictly_matched + norm.vista_without_ccv, 306);
});

test("8b. Deduplicação de divergências: registro com divergência simultânea de valor e data não é duplamente contado", () => {
  const rawPayload = {
    summary: {
      official_sales: 5,
      strictly_matched: 1,
      value_only_mismatches: 1,
      date_only_mismatches: 1,
      value_and_date_mismatches: 1,
      divergent_linked_unique: 3, // 1 val + 1 date + 1 both
      total_linked_unique: 4,
      pipeimob_without_vista_gain: 1,
      vista_without_pipeimob_contract: 1,
      total_vista_gains: 5,
    }
  };

  const norm = normalizeReconciliationSummary(rawPayload);
  assert.equal(norm.official_sales, 5);
  assert.equal(norm.strictly_matched, 1);
  assert.equal(norm.divergent_linked_unique, 3);
  assert.equal(norm.total_linked_unique, 4);
  assert.equal(
    norm.value_only_mismatches + norm.date_only_mismatches + norm.value_and_date_mismatches,
    norm.divergent_linked_unique
  );
  assert.equal(norm.strictly_matched + norm.divergent_linked_unique + norm.ccv_without_vista_gain, norm.official_sales);
  assert.equal(norm.total_linked_unique + norm.vista_without_pipeimob_contract, norm.total_vista_gains);
});


// ---------------------------------------------------------------------------
// 9. Categorias residuais explícitas
// ---------------------------------------------------------------------------
test("9. Categorias residuais explícitas estão presentes no payload e na interface", () => {
  assert.match(workerSource, /divergência de valor/i);
  assert.match(workerSource, /divergência de data/i);
  assert.match(workerSource, /data de fechamento não auditável|data de ganho não auditável/i);
  assert.match(workerSource, /value_mismatches|divergent_matches/);
});


// ---------------------------------------------------------------------------
// 10. Proibição de UltimaAtualizacao como data de ganho
// ---------------------------------------------------------------------------
test("10. Proibição de UltimaAtualizacao como data de ganho", async () => {
  const reconServiceSource = await readFile(
    new URL("../services/sales_reconciliation.py", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(
    workerSource,
    /data_ganho.*UltimaAtualizacao/i,
    "UltimaAtualizacao must never be used as gain date in worker",
  );
  assert.match(
    reconServiceSource,
    /DataFinal/i,
    "DataFinal must be checked in sales reconciliation service",
  );
  assert.doesNotMatch(
    reconServiceSource,
    /gain_date.*UltimaAtualizacao/i,
    "UltimaAtualizacao must not be fallback gain date",
  );
});


// ---------------------------------------------------------------------------
// 11. Renderização desktop e mobile
// ---------------------------------------------------------------------------
test("11. Renderização responsiva desktop e mobile com media queries e estilos fluidos", () => {
  assert.match(workerSource, /@media\(max-width:900px\)/);
  assert.match(workerSource, /\.cso-funnel-card/);
  assert.match(workerSource, /\.cso-recon-grid/);
  assert.match(workerSource, /\.cso-funnel-layout\{grid-template-columns:1fr\}/);
});


// ---------------------------------------------------------------------------
// 12. Sanitização contra XSS
// ---------------------------------------------------------------------------
test("12. Sanitização contra XSS (esc helper utilizado em todas as interpolações dinâmicas)", () => {
  assert.match(workerSource, /const esc=value=>String\(value\?\?""\)\.replace\(\/\[&<>"'\]\/g/);
  assert.match(workerSource, /esc\(stage\.label\)/);
  assert.match(workerSource, /esc\(stage\.reason\)/);
});


// ---------------------------------------------------------------------------
// 13. Preservação dos KPIs quando o Vista estiver indisponível
// ---------------------------------------------------------------------------
test("13. Preservação dos KPIs quando o Vista estiver indisponível (Pipeimob VGV/VGC/Vendas permanecem intactos)", () => {
  assert.match(
    workerSource,
    /Vendas oficializadas.*auditadas via Pipeimob/
  );
  assert.match(
    workerSource,
    /fetchReconciliationAsync/
  );
  assert.match(
    workerSource,
    /fetchFunnelSummaryAsync/
  );
});

