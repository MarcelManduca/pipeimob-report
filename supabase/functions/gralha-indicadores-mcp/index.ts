import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2.57.4";
import {
  buildManagerTeamIndex,
  resolveManagerTeam,
  type ManagerTeamReference,
} from "./team_reference.ts";

const FUNCTION_SLUG = "gralha-indicadores-mcp";
const SERVER_NAME = "Gralha — Indicadores Pipeimob × Vista";
const SERVER_VERSION = "1.12.0";
const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};
const ALLOWED_ROLES = new Set(["super_admin", "viewer"]);

type JsonRpcId = string | number | null;
type JsonRpcRequest = {
  jsonrpc?: string;
  id?: JsonRpcId;
  method?: string;
  params?: Record<string, unknown>;
};

function json(
  body: unknown,
  status = 200,
  headers: HeadersInit = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...headers },
  });
}

function functionBaseUrl(url: URL): string {
  const configured = Deno.env.get("SUPABASE_URL");
  const origin = configured
    ? new URL(configured).origin
    : "https://" + url.host;
  return origin + "/functions/v1/" + FUNCTION_SLUG;
}

function resourceUrl(url: URL): string {
  return functionBaseUrl(url) + "/mcp";
}

function metadataUrl(url: URL): string {
  return functionBaseUrl(url) + "/.well-known/oauth-protected-resource";
}

function unauthorized(url: URL): Response {
  const metadata = metadataUrl(url);
  return json({ error: "authentication_required" }, 401, {
    "WWW-Authenticate": 'Bearer resource_metadata="' + metadata + '"',
  });
}

function rpcResult(id: JsonRpcId | undefined, result: unknown): Response {
  return json({ jsonrpc: "2.0", id: id ?? null, result });
}

function rpcError(
  id: JsonRpcId | undefined,
  code: number,
  message: string,
  data?: unknown,
): Response {
  return json({
    jsonrpc: "2.0",
    id: id ?? null,
    error: { code, message, ...(data === undefined ? {} : { data }) },
  });
}

function textContent(value: unknown) {
  return [{ type: "text", text: JSON.stringify(value) }];
}

function validDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value))
    return false;
  const parsed = Date.parse(`${value}T00:00:00Z`);
  return (
    Number.isFinite(parsed) &&
    new Date(parsed).toISOString().slice(0, 10) === value
  );
}

function validatePeriod(start: unknown, end: unknown): string | null {
  if (!validDate(start) || !validDate(end)) {
    return "Use datas no formato YYYY-MM-DD.";
  }
  const startMs = Date.parse(`${start}T00:00:00Z`);
  const endMs = Date.parse(`${end}T00:00:00Z`);
  const days = (endMs - startMs) / 86_400_000;
  if (days < 0) return "A data inicial não pode ser posterior à data final.";
  if (days > 366) return "O período não pode exceder 366 dias.";
  return null;
}

function todayInSaoPaulo(): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Sao_Paulo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(
    parts.map((part) => [part.type, part.value]),
  );
  return `${values.year}-${values.month}-${values.day}`;
}

function boundedInteger(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isInteger(parsed)) return fallback;
  return Math.min(Math.max(parsed, min), max);
}

function average(values: number[]): number {
  return values.length
    ? values.reduce((sum, value) => sum + value, 0) / values.length
    : 0;
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

async function authorize(request: Request) {
  const header = request.headers.get("Authorization") ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  if (!token) return { ok: false as const, reason: "missing_token" };

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  let publishableKey = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
  const publishableKeysJson = Deno.env.get("SUPABASE_PUBLISHABLE_KEYS");
  if (publishableKeysJson) {
    try {
      const publishableKeys = JSON.parse(publishableKeysJson) as Record<
        string,
        unknown
      >;
      const selected =
        (typeof publishableKeys.default === "string" &&
          publishableKeys.default) ||
        Object.values(publishableKeys).find(
          (value) => typeof value === "string",
        );
      if (typeof selected === "string") publishableKey = selected;
    } catch {
      console.error("publishable_keys_parse_failed");
    }
  }
  if (!supabaseUrl || !publishableKey) {
    return { ok: false as const, reason: "server_configuration_error" };
  }

  const userClient = createClient(supabaseUrl, publishableKey, {
    global: { headers: { Authorization: "Bearer " + token } },
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data: userData, error: userError } =
    await userClient.auth.getUser(token);
  const user = userData.user;
  if (userError || !user)
    return { ok: false as const, reason: "invalid_token" };

  const [
    { data: profile, error: profileError },
    { data: roles, error: rolesError },
  ] = await Promise.all([
    userClient
      .from("profiles")
      .select("status")
      .eq("id", user.id)
      .maybeSingle(),
    userClient.from("user_roles").select("role").eq("user_id", user.id),
  ]);
  if (profileError || rolesError) {
    console.error("authorization_lookup_failed", {
      profileCode: profileError?.code ?? null,
      rolesCode: rolesError?.code ?? null,
    });
    return { ok: false as const, reason: "authorization_lookup_failed" };
  }

  const role = roles?.find(
    (candidate) =>
      typeof candidate.role === "string" && ALLOWED_ROLES.has(candidate.role),
  )?.role;
  if (profile?.status !== "active" || !role) {
    return { ok: false as const, reason: "access_denied" };
  }

  return { ok: true as const, token, userId: user.id, role, userClient };
}

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function normalizeBrokerName(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase("pt-BR");
}

function normalizePropertyCode(value: unknown): string {
  return String(value ?? "")
    .replace(/\s+/g, "")
    .trim()
    .toLocaleUpperCase("pt-BR");
}

async function fetchBackendJson(
  endpoint: URL,
  token: string,
): Promise<{ reachable: boolean; status: number; payload: unknown }> {
  try {
    const response = await fetch(endpoint, {
      headers: {
        Accept: "application/json",
        Authorization: "Bearer " + token,
      },
      signal: AbortSignal.timeout(35_000),
    });
    const raw = await response.text();
    let payload: unknown = null;
    try {
      payload = raw ? JSON.parse(raw) : null;
    } catch {
      // Never return an unstructured upstream body to the model.
    }
    return { reachable: true, status: response.status, payload };
  } catch {
    return { reachable: false, status: 0, payload: null };
  }
}

async function callSalesRanking(
  token: string,
  args: Record<string, unknown>,
  userClient: ReturnType<typeof createClient>,
): Promise<{ isError: boolean; value: unknown }> {
  const requestedStart = args.data_inicio ?? args.data_inicio_ccv;
  const requestedEnd = args.data_fim ?? args.data_fim_ccv;
  const validationError = validatePeriod(requestedStart, requestedEnd);
  if (validationError) {
    return {
      isError: true,
      value: { error: "invalid_period", detail: validationError },
    };
  }

  const today = todayInSaoPaulo();
  const start = String(requestedStart);
  const end = String(requestedEnd) > today ? today : String(requestedEnd);
  if (start > end) {
    return {
      isError: true,
      value: {
        error: "future_period",
        detail:
          "O período solicitado começa no futuro e ainda não possui dados.",
        requested_period: { start, end: String(requestedEnd) },
        current_date: today,
      },
    };
  }
  const periodCoverage = {
    requested: { start, end: String(requestedEnd) },
    effective: { start, end },
    current_date: today,
    future_end_clamped: end !== String(requestedEnd),
  };

  const requestedMetric = args.criterio ?? args.metric ?? "quantidade";
  const metric = requestedMetric === "vgv" ? "vgv" : "sales_count";
  if (!["quantidade", "sales_count", "vgv"].includes(String(requestedMetric))) {
    return {
      isError: true,
      value: {
        error: "invalid_metric",
        detail: "O critério deve ser quantidade ou vgv.",
      },
    };
  }

  const brokerFilter =
    typeof args.corretor === "string" && args.corretor.trim()
      ? args.corretor.replace(/\s+/g, " ").trim()
      : null;
  const teamFilter =
    typeof args.equipe === "string" && args.equipe.trim()
      ? args.equipe.replace(/\s+/g, " ").trim()
      : null;
  const topN = boundedInteger(args.top_n, 10, 1, 50);
  const requestedGroup = String(args.agrupar_por ?? "corretor");
  const groupBy =
    requestedGroup === "equipe"
      ? "equipe"
      : requestedGroup === "bairro" || brokerFilter
        ? "bairro"
        : "corretor";

  const backend = (
    Deno.env.get("MCP_PIPEIMOB_BACKEND_URL") ??
    "https://pipeimob-report.onrender.com"
  ).replace(/\/+$/, "");
  const reconciliationEndpoint = new URL(backend + "/api/reconciliation/sales");
  reconciliationEndpoint.searchParams.set("data_inicio_ccv", String(start));
  reconciliationEndpoint.searchParams.set("data_fim_ccv", String(end));

  const reconciliation = await fetchBackendJson(reconciliationEndpoint, token);
  if (!reconciliation.reachable) {
    return {
      isError: true,
      value: {
        error: "backend_unreachable",
        detail: "O serviço de conciliação não respondeu no tempo esperado.",
      },
    };
  }
  if (reconciliation.status < 200 || reconciliation.status >= 300) {
    return {
      isError: true,
      value: {
        error:
          reconciliation.status === 401
            ? "backend_auth_rejected"
            : "indicator_unavailable",
        status: reconciliation.status,
        detail:
          reconciliation.status === 401
            ? "O backend não reconheceu a identidade OAuth deste conector."
            : "Não foi possível concluir a consulta conciliada.",
      },
    };
  }
  if (!reconciliation.payload || typeof reconciliation.payload !== "object") {
    return {
      isError: true,
      value: {
        error: "invalid_upstream_contract",
        detail: "A conciliação respondeu em um formato inesperado.",
      },
    };
  }

  const root = reconciliation.payload as Record<string, unknown>;
  const items = Array.isArray(root.items) ? root.items : [];
  const summary =
    root.summary && typeof root.summary === "object"
      ? (root.summary as Record<string, unknown>)
      : {};

  if (groupBy === "equipe") {
    const { data: referenceRows, error: referenceError } = await userClient
      .from("manager_team_reference")
      .select(
        "manager_key,manager_name,team_key,team_name,valid_from,valid_to,source_updated_through,review_required",
      )
      .lte("valid_from", String(end))
      .or("valid_to.is.null,valid_to.gte." + String(start))
      .order("manager_key", { ascending: true })
      .order("valid_from", { ascending: true })
      .limit(1000);

    if (referenceError) {
      console.error("team_reference_lookup_failed", {
        code: referenceError.code,
      });
      return {
        isError: true,
        value: {
          error: "team_reference_unavailable",
          detail:
            "O vínculo gerencial entre responsáveis e equipes não pôde ser consultado.",
        },
      };
    }

    const references = (referenceRows ?? []) as ManagerTeamReference[];
    const historyByManager = buildManagerTeamIndex(references);
    let sourceUpdatedThrough: string | null = null;

    for (const reference of references) {
      const updatedThrough = String(
        reference.source_updated_through ?? "",
      ).slice(0, 10);
      if (
        updatedThrough &&
        (!sourceUpdatedThrough || updatedThrough > sourceUpdatedThrough)
      ) {
        sourceUpdatedThrough = updatedThrough;
      }
    }

    const grouped = new Map<
      string,
      { team: string; sales_count: number; vgv: number }
    >();
    const officialIds = new Set<string>();
    let attributedSales = 0;
    let attributedVgv = 0;
    let assignedSales = 0;
    let assignedVgv = 0;
    let apiTeamSales = 0;
    let managerReferenceSales = 0;
    let managerReferenceReviewSales = 0;
    let ambiguousManagerReferenceSales = 0;
    let apiTeamConflictSales = 0;

    for (const candidate of items) {
      if (!candidate || typeof candidate !== "object") continue;
      const item = candidate as Record<string, unknown>;
      const transactionId =
        typeof item.pipeimob_transaction_id === "string"
          ? item.pipeimob_transaction_id.trim()
          : "";
      if (!transactionId || officialIds.has(transactionId)) continue;

      officialIds.add(transactionId);
      const value = asNumber(item.official_value) ?? 0;
      attributedSales += 1;
      attributedVgv += value;

      const saleDate =
        typeof item.official_sale_date === "string"
          ? item.official_sale_date.slice(0, 10)
          : "";
      let teamName =
        typeof item.team_name === "string"
          ? item.team_name.replace(/\s+/g, " ").trim()
          : "";
      let teamKey = teamName ? normalizeBrokerName(teamName) : "";

      if (item.team_resolution_status === "conflict_api_sources") {
        apiTeamConflictSales += 1;
      }

      if (teamName) {
        apiTeamSales += 1;
      } else {
        const manager =
          typeof item.responsible_manager === "string"
            ? item.responsible_manager.replace(/\s+/g, " ").trim()
            : typeof item.fiscal_broker === "string"
              ? item.fiscal_broker.replace(/\s+/g, " ").trim()
              : "";
        const assignment = resolveManagerTeam(
          historyByManager,
          manager,
          saleDate,
        );
        if (assignment.status === "resolved") {
          teamName = assignment.teamName;
          teamKey = assignment.teamKey;
          managerReferenceSales += 1;
          if (assignment.reviewRequired) managerReferenceReviewSales += 1;
        } else if (assignment.status === "ambiguous") {
          ambiguousManagerReferenceSales += 1;
        }
      }

      if (!teamName || !teamKey) continue;
      assignedSales += 1;
      assignedVgv += value;
      const row = grouped.get(teamKey) ?? {
        team: teamName,
        sales_count: 0,
        vgv: 0,
      };
      row.sales_count += 1;
      row.vgv += value;
      grouped.set(teamKey, row);
    }

    const ranking = [...grouped.values()];
    ranking.sort((a, b) => {
      if (metric === "vgv") {
        return (
          b.vgv - a.vgv ||
          b.sales_count - a.sales_count ||
          a.team.localeCompare(b.team, "pt-BR")
        );
      }
      return (
        b.sales_count - a.sales_count ||
        b.vgv - a.vgv ||
        a.team.localeCompare(b.team, "pt-BR")
      );
    });

    const completeRanking = ranking.map((row, index) => ({
      position: index + 1,
      team: row.team,
      sales_count: row.sales_count,
      vgv: row.vgv,
      average_ticket: row.sales_count > 0 ? row.vgv / row.sales_count : 0,
    }));
    const normalizedTeamFilter = teamFilter
      ? normalizeBrokerName(teamFilter)
      : null;
    const selectedRanking = normalizedTeamFilter
      ? completeRanking.filter((row) => {
          const normalized = normalizeBrokerName(row.team);
          return (
            normalized === normalizedTeamFilter ||
            normalized.includes(normalizedTeamFilter)
          );
        })
      : completeRanking.slice(0, topN);

    const rankingBySales = [...ranking].sort(
      (a, b) =>
        b.sales_count - a.sales_count ||
        b.vgv - a.vgv ||
        a.team.localeCompare(b.team, "pt-BR"),
    );
    const rankingByVgv = [...ranking].sort(
      (a, b) =>
        b.vgv - a.vgv ||
        b.sales_count - a.sales_count ||
        a.team.localeCompare(b.team, "pt-BR"),
    );
    const selectedTeam = normalizedTeamFilter
      ? ranking.find((row) => {
          const normalized = normalizeBrokerName(row.team);
          return (
            normalized === normalizedTeamFilter ||
            normalized.includes(normalizedTeamFilter)
          );
        })
      : null;
    if (normalizedTeamFilter && !selectedTeam) {
      return {
        isError: true,
        value: {
          error: "team_not_found",
          detail:
            "Nenhuma equipe atribuída corresponde ao nome informado no período.",
          team_filter: teamFilter,
          period: root.period ?? { start, end },
          coverage: periodCoverage,
        },
      };
    }
    const teamEvaluation = selectedTeam
      ? {
          team: selectedTeam.team,
          sales_count: selectedTeam.sales_count,
          vgv: selectedTeam.vgv,
          average_ticket:
            selectedTeam.sales_count > 0
              ? selectedTeam.vgv / selectedTeam.sales_count
              : 0,
          positions: {
            sales_count:
              rankingBySales.findIndex((row) => row === selectedTeam) + 1,
            vgv: rankingByVgv.findIndex((row) => row === selectedTeam) + 1,
            total_teams: ranking.length,
          },
          benchmarks: {
            sales_share:
              assignedSales > 0 ? selectedTeam.sales_count / assignedSales : 0,
            vgv_share: assignedVgv > 0 ? selectedTeam.vgv / assignedVgv : 0,
            average_team_sales: average(ranking.map((row) => row.sales_count)),
            median_team_sales: median(ranking.map((row) => row.sales_count)),
            average_team_vgv: average(ranking.map((row) => row.vgv)),
            median_team_vgv: median(ranking.map((row) => row.vgv)),
            gap_to_sales_leader: Math.max(
              (rankingBySales[0]?.sales_count ?? 0) - selectedTeam.sales_count,
              0,
            ),
            gap_to_vgv_leader: Math.max(
              (rankingByVgv[0]?.vgv ?? 0) - selectedTeam.vgv,
              0,
            ),
          },
          evidence_scope: [
            "official_sales",
            "vgv",
            "average_ticket",
            "team_assignment",
          ],
          unavailable_without_operational_data: [
            "pipeline_conversion",
            "visit_to_sale_conversion",
            "proposal_to_sale_conversion",
          ],
        }
      : null;

    const chartRows = completeRanking.slice(0, Math.min(topN, 10));

    return {
      isError: false,
      value: {
        contract_version: "3.0",
        official_source: root.official_source ?? "pipeimob_api_v2",
        commercial_source: root.commercial_source ?? "vista_negocio_ganho",
        team_reference_source: "management_spreadsheet_manager_team_dimension",
        attribution: "api_team_then_date_aware_manager_reference",
        group_by: "team",
        metric,
        period: root.period ?? { start, end },
        coverage: {
          ...periodCoverage,
          team_reference_updated_through: sourceUpdatedThrough,
          team_reference_covers_effective_end: Boolean(
            sourceUpdatedThrough && sourceUpdatedThrough >= end,
          ),
        },
        generated_at: root.generated_at ?? new Date().toISOString(),
        source_updated_through: sourceUpdatedThrough,
        summary: {
          attributed_sales: attributedSales,
          attributed_vgv: attributedVgv,
          assigned_sales: assignedSales,
          assigned_vgv: assignedVgv,
          sales_without_team: Math.max(attributedSales - assignedSales, 0),
          team_assignment_rate:
            attributedSales > 0 ? assignedSales / attributedSales : 0,
          api_team_sales: apiTeamSales,
          api_team_conflict_sales: apiTeamConflictSales,
          manager_reference_sales: managerReferenceSales,
          manager_reference_review_sales: managerReferenceReviewSales,
          ambiguous_manager_reference_sales: ambiguousManagerReferenceSales,
        },
        total_ranked: completeRanking.length,
        top_n: topN,
        team_filter: teamFilter,
        team_evaluation: teamEvaluation,
        ranking: selectedRanking,
        visualization: {
          schema_version: "1.0",
          type: "bar",
          title:
            metric === "vgv"
              ? `Top ${chartRows.length} equipes por VGV`
              : `Top ${chartRows.length} equipes por quantidade de vendas`,
          metric,
          unit: metric === "vgv" ? "BRL" : "sales",
          series: chartRows.map((row) => ({
            label: row.team,
            value: metric === "vgv" ? row.vgv : row.sales_count,
            sales_count: row.sales_count,
            vgv: row.vgv,
          })),
          footnote: `Período efetivo: ${start} a ${end}. ${assignedSales} de ${attributedSales} vendas possuem equipe atribuída.`,
        },
      },
    };
  }

  if (groupBy === "bairro") {
    const transactionsEndpoint = new URL(backend + "/api/transactions");
    transactionsEndpoint.searchParams.set("data_inicio_ccv", String(start));
    transactionsEndpoint.searchParams.set("data_fim_ccv", String(end));
    const transactionsResult = await fetchBackendJson(
      transactionsEndpoint,
      token,
    );
    if (!transactionsResult.reachable) {
      return {
        isError: true,
        value: {
          error: "backend_unreachable",
          detail: "O detalhamento de bairros não respondeu no tempo esperado.",
        },
      };
    }
    if (transactionsResult.status < 200 || transactionsResult.status >= 300) {
      return {
        isError: true,
        value: {
          error:
            transactionsResult.status === 401
              ? "backend_auth_rejected"
              : "neighborhood_detail_unavailable",
          status: transactionsResult.status,
          detail: "Não foi possível consultar os bairros das vendas oficiais.",
        },
      };
    }

    const transactionsRoot =
      transactionsResult.payload &&
      typeof transactionsResult.payload === "object"
        ? (transactionsResult.payload as Record<string, unknown>)
        : {};
    const transactionsData =
      transactionsRoot.data && typeof transactionsRoot.data === "object"
        ? (transactionsRoot.data as Record<string, unknown>)
        : {};
    const transactions = Array.isArray(transactionsData.transactions)
      ? transactionsData.transactions
      : [];

    const neighborhoodByTransaction = new Map<string, string>();
    for (const candidate of transactions) {
      if (!candidate || typeof candidate !== "object") continue;
      const transaction = candidate as Record<string, unknown>;
      const transactionId =
        typeof transaction.transacao_unique_id_pipeimob === "string"
          ? transaction.transacao_unique_id_pipeimob.trim()
          : "";
      const neighborhood =
        typeof transaction.endereco_bairro === "string"
          ? transaction.endereco_bairro.replace(/\s+/g, " ").trim()
          : "";
      if (transactionId && neighborhood) {
        neighborhoodByTransaction.set(transactionId, neighborhood);
      }
    }

    const requestedBroker = brokerFilter
      ? normalizeBrokerName(brokerFilter)
      : null;
    const matchedBrokers = new Set<string>();
    const officialIds = new Set<string>();
    const grouped = new Map<
      string,
      { neighborhood: string; sales_count: number; vgv: number }
    >();
    let missingNeighborhoodSales = 0;
    let attributedSales = 0;
    let attributedVgv = 0;

    for (const candidate of items) {
      if (!candidate || typeof candidate !== "object") continue;
      const item = candidate as Record<string, unknown>;
      const transactionId =
        typeof item.pipeimob_transaction_id === "string"
          ? item.pipeimob_transaction_id.trim()
          : "";
      if (!transactionId || officialIds.has(transactionId)) continue;

      const broker =
        typeof item.commercial_broker === "string"
          ? item.commercial_broker.replace(/\s+/g, " ").trim()
          : "";
      if (!broker) continue;
      const normalizedBroker = normalizeBrokerName(broker);
      if (
        requestedBroker &&
        normalizedBroker !== requestedBroker &&
        !normalizedBroker.includes(requestedBroker)
      ) {
        continue;
      }

      officialIds.add(transactionId);
      matchedBrokers.add(broker);
      const value = asNumber(item.official_value) ?? 0;
      attributedSales += 1;
      attributedVgv += value;

      const neighborhood = neighborhoodByTransaction.get(transactionId);
      if (!neighborhood) {
        missingNeighborhoodSales += 1;
        continue;
      }
      const key = normalizeBrokerName(neighborhood);
      const row = grouped.get(key) ?? {
        neighborhood,
        sales_count: 0,
        vgv: 0,
      };
      row.sales_count += 1;
      row.vgv += value;
      grouped.set(key, row);
    }

    if (requestedBroker && matchedBrokers.size === 0) {
      return {
        isError: true,
        value: {
          error: "broker_not_found",
          detail:
            "Nenhuma venda atribuída foi encontrada para o corretor informado no período.",
          broker_filter: brokerFilter,
          period: root.period ?? { start, end },
        },
      };
    }

    const ranking = [...grouped.values()];
    ranking.sort((a, b) => {
      if (metric === "vgv") {
        return (
          b.vgv - a.vgv ||
          b.sales_count - a.sales_count ||
          a.neighborhood.localeCompare(b.neighborhood, "pt-BR")
        );
      }
      return (
        b.sales_count - a.sales_count ||
        b.vgv - a.vgv ||
        a.neighborhood.localeCompare(b.neighborhood, "pt-BR")
      );
    });
    const completeRanking = ranking.map((row, index) => ({
      position: index + 1,
      neighborhood: row.neighborhood,
      sales_count: row.sales_count,
      vgv: row.vgv,
      average_ticket: row.sales_count > 0 ? row.vgv / row.sales_count : 0,
    }));
    const selectedRanking = completeRanking.slice(0, topN);
    const chartRows = selectedRanking.slice(0, 10);

    return {
      isError: false,
      value: {
        contract_version: "3.0",
        official_source: root.official_source ?? "pipeimob_api_v2",
        commercial_source: root.commercial_source ?? "vista_negocio_ganho",
        attribution: "vista_commercial_broker",
        group_by: "neighborhood",
        broker_filter: brokerFilter,
        matched_brokers: [...matchedBrokers].sort((a, b) =>
          a.localeCompare(b, "pt-BR"),
        ),
        metric,
        period: root.period ?? { start, end },
        coverage: periodCoverage,
        generated_at: root.generated_at ?? new Date().toISOString(),
        summary: {
          attributed_sales: attributedSales,
          attributed_vgv: attributedVgv,
          sales_with_neighborhood: attributedSales - missingNeighborhoodSales,
          sales_without_neighborhood: missingNeighborhoodSales,
        },
        total_ranked: completeRanking.length,
        top_n: topN,
        ranking: selectedRanking,
        visualization: {
          schema_version: "1.0",
          type: "bar",
          title:
            metric === "vgv"
              ? `Top ${chartRows.length} bairros por VGV`
              : `Top ${chartRows.length} bairros por quantidade de vendas`,
          metric,
          unit: metric === "vgv" ? "BRL" : "sales",
          series: chartRows.map((row) => ({
            label: row.neighborhood,
            value: metric === "vgv" ? row.vgv : row.sales_count,
            sales_count: row.sales_count,
            vgv: row.vgv,
          })),
          footnote: `Período efetivo: ${start} a ${end}. ${attributedSales - missingNeighborhoodSales} de ${attributedSales} vendas possuem bairro informado.`,
        },
      },
    };
  }

  const grouped = new Map<
    string,
    { commercial_broker: string; sales_count: number; vgv: number }
  >();
  const officialIds = new Set<string>();
  const attributedIds = new Set<string>();
  let derivedOfficialVgv = 0;
  let attributedVgv = 0;

  for (const candidate of items) {
    if (!candidate || typeof candidate !== "object") continue;
    const item = candidate as Record<string, unknown>;
    const transactionId =
      typeof item.pipeimob_transaction_id === "string"
        ? item.pipeimob_transaction_id.trim()
        : "";
    if (!transactionId || officialIds.has(transactionId)) continue;

    officialIds.add(transactionId);
    const value = asNumber(item.official_value) ?? 0;
    derivedOfficialVgv += value;

    const broker =
      typeof item.commercial_broker === "string"
        ? item.commercial_broker.replace(/\s+/g, " ").trim()
        : "";
    if (!broker) continue;

    attributedIds.add(transactionId);
    attributedVgv += value;
    const key = normalizeBrokerName(broker);
    const row = grouped.get(key) ?? {
      commercial_broker: broker,
      sales_count: 0,
      vgv: 0,
    };
    row.sales_count += 1;
    row.vgv += value;
    grouped.set(key, row);
  }

  const ranking = [...grouped.values()];
  ranking.sort((a, b) => {
    if (metric === "vgv") {
      return (
        b.vgv - a.vgv ||
        b.sales_count - a.sales_count ||
        a.commercial_broker.localeCompare(b.commercial_broker, "pt-BR")
      );
    }
    return (
      b.sales_count - a.sales_count ||
      b.vgv - a.vgv ||
      a.commercial_broker.localeCompare(b.commercial_broker, "pt-BR")
    );
  });
  const completeRanking = ranking.map((row, index) => ({
    position: index + 1,
    commercial_broker: row.commercial_broker,
    sales_count: row.sales_count,
    vgv: row.vgv,
    average_ticket: row.sales_count > 0 ? row.vgv / row.sales_count : 0,
  }));
  const selectedRanking = completeRanking.slice(0, topN);
  const chartRows = selectedRanking.slice(0, 10);

  const officialSales = asNumber(summary.official_sales) ?? officialIds.size;
  const officialVgv = asNumber(summary.official_vgv) ?? derivedOfficialVgv;
  return {
    isError: false,
    value: {
      contract_version: "3.0",
      official_source: root.official_source ?? "pipeimob_api_v2",
      commercial_source: root.commercial_source ?? "vista_negocio_ganho",
      attribution: "vista_commercial_broker",
      group_by: "commercial_broker",
      metric,
      period: root.period ?? { start, end },
      coverage: periodCoverage,
      generated_at: root.generated_at ?? new Date().toISOString(),
      summary: {
        official_sales: officialSales,
        official_vgv: officialVgv,
        attributed_sales: attributedIds.size,
        attributed_vgv: attributedVgv,
        unattributed_sales: Math.max(officialSales - attributedIds.size, 0),
        unattributed_vgv: Math.max(officialVgv - attributedVgv, 0),
      },
      total_ranked: completeRanking.length,
      top_n: topN,
      ranking: selectedRanking,
      visualization: {
        schema_version: "1.0",
        type: "bar",
        title:
          metric === "vgv"
            ? `Top ${chartRows.length} corretores por VGV`
            : `Top ${chartRows.length} corretores por quantidade de vendas`,
        metric,
        unit: metric === "vgv" ? "BRL" : "sales",
        series: chartRows.map((row) => ({
          label: row.commercial_broker,
          value: metric === "vgv" ? row.vgv : row.sales_count,
          sales_count: row.sales_count,
          vgv: row.vgv,
        })),
        footnote: `Período efetivo: ${start} a ${end}. ${attributedIds.size} de ${officialSales} vendas possuem corretor comercial atribuído.`,
      },
    },
  };
}

async function callVistaFunnelCohort(
  token: string,
  args: Record<string, unknown>,
  userClient: ReturnType<typeof createClient>,
): Promise<{ isError: boolean; value: unknown }> {
  const requestedStart = args.data_inicio;
  const requestedEnd = args.data_fim;
  const validationError = validatePeriod(requestedStart, requestedEnd);
  if (validationError) {
    return {
      isError: true,
      value: { error: "invalid_period", detail: validationError },
    };
  }

  const today = todayInSaoPaulo();
  const start = String(requestedStart);
  const requestedEndText = String(requestedEnd);
  const end = requestedEndText > today ? today : requestedEndText;
  const requestedGroup = String(args.agrupar_por ?? "nenhum");
  if (!["nenhum", "equipe"].includes(requestedGroup)) {
    return {
      isError: true,
      value: {
        error: "invalid_group",
        detail: "O agrupamento do funil deve ser nenhum ou equipe.",
      },
    };
  }
  const groupBy = requestedGroup === "equipe" ? "equipe" : "nenhum";
  if (start > end) {
    return {
      isError: true,
      value: {
        error: "future_period",
        detail: "O período solicitado começa no futuro e ainda não possui dados.",
        requested_period: { start, end: requestedEndText },
        current_date: today,
      },
    };
  }

  const backend = (
    Deno.env.get("MCP_PIPEIMOB_BACKEND_URL") ??
    "https://pipeimob-report.onrender.com"
  ).replace(/\/+$/, "");
  const endpoint = new URL(backend + "/api/vista/funnel/cohort");
  endpoint.searchParams.set("data_inicio", start);
  endpoint.searchParams.set("data_fim", end);

  const result = await fetchBackendJson(endpoint, token);
  if (!result.reachable) {
    return {
      isError: true,
      value: {
        error: "backend_unreachable",
        detail: "A consulta de funil do Vista não respondeu no tempo esperado.",
      },
    };
  }
  if (result.status < 200 || result.status >= 300) {
    const upstream = result.payload && typeof result.payload === "object"
      ? result.payload as Record<string, unknown>
      : {};
    console.warn("vista_funnel_backend_failed", {
      status: result.status,
      errorCode: typeof upstream.error_code === "string"
        ? upstream.error_code
        : null,
    });
    return {
      isError: true,
      value: {
        error:
          result.status === 401
            ? "backend_auth_rejected"
            : "vista_funnel_unavailable",
        status: result.status,
        detail:
          result.status === 401
            ? "O backend não reconheceu a identidade OAuth deste conector."
            : "Os dados agregados do funil do Vista estão temporariamente indisponíveis.",
      },
    };
  }
  if (!result.payload || typeof result.payload !== "object") {
    return {
      isError: true,
      value: {
        error: "invalid_upstream_contract",
        detail: "O funil do Vista respondeu em um formato inesperado.",
      },
    };
  }

  const root = result.payload as Record<string, unknown>;
  const summary =
    root.summary && typeof root.summary === "object"
      ? (root.summary as Record<string, unknown>)
      : {};
  const upstreamProposal =
    summary.proposal && typeof summary.proposal === "object"
      ? (summary.proposal as Record<string, unknown>)
      : {};
  const assignmentBreakdown = Array.isArray(
      upstreamProposal.assignment_breakdown,
    )
    ? upstreamProposal.assignment_breakdown
    : [];
  const {
    assignment_breakdown: _privateAssignmentBreakdown,
    ...safeUpstreamProposal
  } = upstreamProposal;

  let proposalTeamBreakdown: Array<{
    team: string;
    open_deals_count: number;
    current_stage_deals_count: number;
  }> = [];
  let proposalTeamCoverage: Record<string, unknown> | null = null;
  let visualization: Record<string, unknown> | null = null;

  if (groupBy === "equipe") {
    const { data: referenceRows, error: referenceError } = await userClient
      .from("manager_team_reference")
      .select(
        "manager_key,manager_name,team_key,team_name,valid_from,valid_to,source_updated_through,review_required",
      )
      .lte("valid_from", end)
      .or("valid_to.is.null,valid_to.gte." + start)
      .order("manager_key", { ascending: true })
      .order("valid_from", { ascending: true })
      .limit(1000);

    if (referenceError) {
      console.error("proposal_team_reference_lookup_failed", {
        code: referenceError.code,
      });
      return {
        isError: true,
        value: {
          error: "team_reference_unavailable",
          detail:
            "A separação das propostas por equipe não pôde consultar o vínculo gerencial.",
        },
      };
    }

    const references = (referenceRows ?? []) as ManagerTeamReference[];
    const historyByManager = buildManagerTeamIndex(references);
    const grouped = new Map<
      string,
      {
        team: string;
        open_deals_count: number;
        current_stage_deals_count: number;
      }
    >();
    let sourceUpdatedThrough: string | null = null;
    let assignedOpen = 0;
    let assignedCurrent = 0;
    let apiTeamOpen = 0;
    let managerReferenceOpen = 0;
    let managerReferenceReviewOpen = 0;
    let ambiguousOpen = 0;
    let unresolvedOpen = 0;

    for (const reference of references) {
      const updatedThrough = String(
        reference.source_updated_through ?? "",
      ).slice(0, 10);
      if (
        updatedThrough &&
        (!sourceUpdatedThrough || updatedThrough > sourceUpdatedThrough)
      ) {
        sourceUpdatedThrough = updatedThrough;
      }
    }

    for (const candidate of assignmentBreakdown) {
      if (!candidate || typeof candidate !== "object") continue;
      const row = candidate as Record<string, unknown>;
      const openCount = Math.max(0, asNumber(row.open_deals_count) ?? 0);
      const currentCount = Math.max(
        0,
        asNumber(row.current_stage_deals_count) ?? 0,
      );
      let teamName =
        typeof row.team === "string"
          ? row.team.replace(/\s+/g, " ").trim()
          : "";
      let teamKey = teamName ? normalizeBrokerName(teamName) : "";

      if (teamName) {
        apiTeamOpen += openCount;
      } else {
        const responsible =
          typeof row.responsible === "string"
            ? row.responsible.replace(/\s+/g, " ").trim()
            : "";
        const createdDate =
          typeof row.created_date === "string"
            ? row.created_date.slice(0, 10)
            : "";
        const assignment = resolveManagerTeam(
          historyByManager,
          responsible,
          createdDate,
        );
        if (assignment.status === "resolved") {
          teamName = assignment.teamName;
          teamKey = assignment.teamKey;
          managerReferenceOpen += openCount;
          if (assignment.reviewRequired) {
            managerReferenceReviewOpen += openCount;
          }
        } else if (assignment.status === "ambiguous") {
          ambiguousOpen += openCount;
        } else {
          unresolvedOpen += openCount;
        }
      }

      if (!teamName || !teamKey) continue;
      assignedOpen += openCount;
      assignedCurrent += currentCount;
      const groupedRow = grouped.get(teamKey) ?? {
        team: teamName,
        open_deals_count: 0,
        current_stage_deals_count: 0,
      };
      groupedRow.open_deals_count += openCount;
      groupedRow.current_stage_deals_count += currentCount;
      grouped.set(teamKey, groupedRow);
    }

    proposalTeamBreakdown = [...grouped.values()].sort((a, b) =>
      b.open_deals_count - a.open_deals_count ||
      b.current_stage_deals_count - a.current_stage_deals_count ||
      a.team.localeCompare(b.team, "pt-BR")
    );
    const proposalOpenTotal = Math.max(
      0,
      asNumber(
        upstreamProposal.created_deals_in_proposal_stage_with_open_status,
      ) ?? 0,
    );
    const proposalCurrentTotal = Math.max(
      0,
      asNumber(upstreamProposal.created_deals_currently_in_proposal) ?? 0,
    );
    proposalTeamCoverage = {
      proposal_open_total: proposalOpenTotal,
      proposal_current_stage_total: proposalCurrentTotal,
      assigned_open: assignedOpen,
      unassigned_open: Math.max(0, proposalOpenTotal - assignedOpen),
      assigned_current_stage: assignedCurrent,
      unassigned_current_stage: Math.max(
        0,
        proposalCurrentTotal - assignedCurrent,
      ),
      open_assignment_rate: proposalOpenTotal
        ? assignedOpen / proposalOpenTotal
        : 0,
      api_team_open: apiTeamOpen,
      manager_reference_open: managerReferenceOpen,
      manager_reference_review_open: managerReferenceReviewOpen,
      ambiguous_manager_reference_open: ambiguousOpen,
      unresolved_open: unresolvedOpen,
      reference_updated_through: sourceUpdatedThrough,
    };
    visualization = {
      type: "bar",
      title: "Propostas em aberto por equipe",
      metric: "sales_count",
      unit: "sales",
      series: proposalTeamBreakdown.slice(0, 10).map((row) => ({
        label: row.team,
        value: row.open_deals_count,
        sales_count: row.open_deals_count,
        current_stage_deals_count: row.current_stage_deals_count,
        vgv: null,
      })),
      footnote:
        `${assignedOpen} de ${proposalOpenTotal} propostas em aberto possuem equipe atribuída. ` +
        "Fotografia atual dos negócios criados no período.",
    };
  }

  return {
    isError: false,
    value: {
      contract_version: "1.2",
      source: root.source ?? "vista_negocios_listar",
      group_by: groupBy,
      period: root.period ?? { start, end, basis: "DataInicial" },
      coverage: {
        requested: { start, end: requestedEndText },
        effective: { start, end },
        current_date: today,
        future_end_clamped: end !== requestedEndText,
      },
      generated_at: root.generated_at ?? new Date().toISOString(),
      semantics: {
        cohort: "distinct_deals_created_in_period",
        stage_breakdown: "current_stage_at_query_time",
        stage_entry_events_available: false,
        proposals_generated_in_period_available: false,
        warning:
          "Negócios criados no período e atualmente em Proposta não equivalem a entradas na etapa Proposta durante o período.",
      },
      summary: {
        ...summary,
        proposal: {
          ...safeUpstreamProposal,
          ...(groupBy === "equipe"
            ? {
              team_breakdown: proposalTeamBreakdown,
              team_coverage: proposalTeamCoverage,
            }
            : {}),
          proposals_generated_in_period: null,
          proposals_generated_status: "requires_stage_event_history",
        },
      },
      supported_questions: [
        "quantos negócios distintos foram criados no período",
        "qual é a situação geral atual desses negócios",
        "como esses negócios estão distribuídos pelas etapas atuais",
        "quantos negócios criados no período estão atualmente em Proposta",
        "quantos negócios da etapa atual Proposta possuem status geral Em aberto",
        "como os negócios atualmente em Proposta se distribuem por equipe",
      ],
      unsupported_without_stage_history: [
        "quantas propostas foram geradas no período",
        "quantos negócios entraram em cada etapa durante o período",
        "conversão entre etapas baseada em eventos históricos",
        "tempo real de passagem entre etapas",
      ],
      response_guidance: {
        answer_style: "direct_then_managerial_when_relevant",
        lead_with_direct_answer: true,
        required_prefix: null,
        objective_answer_max_sentences: 2,
        unavailable_metric_max_words: 80,
        management_snapshot_order: [
          "current_stage_open_status",
          "current_stage_total",
          "historical_metric_limitation",
        ],
        management_context_only_when_relevant: true,
        include_verified_current_snapshot_alternative: true,
        avoid_internal_implementation_terms: true,
        do_not_repeat_user_question: true,
        never_say_contract_confirmation_is_pending: true,
      },
      visualization,
    },
  };
}

const TOOLS = [
  {
    name: "consultar_ranking_vendas",
    title: "Consultar vendas por corretor, equipe ou bairro",
    description:
      "Consulta vendas oficiais em um período. Pode ranquear corretores, equipes ou bairros e filtrar bairros por corretor. Conta somente contratos oficiais assinados no Pipeimob, atribui o corretor comercial pelo Vista e resolve equipe primeiro pelas APIs e depois pela referência gerencial vigente.",
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        data_inicio: {
          type: "string",
          format: "date",
          description: "Data inicial no formato YYYY-MM-DD.",
        },
        data_fim: {
          type: "string",
          format: "date",
          description: "Data final no formato YYYY-MM-DD.",
        },
        criterio: {
          type: "string",
          enum: ["quantidade", "vgv"],
          default: "quantidade",
          description: "Ordena por quantidade de vendas ou por VGV.",
        },
        agrupar_por: {
          type: "string",
          enum: ["corretor", "equipe", "bairro"],
          default: "corretor",
          description:
            "Use bairro para perguntas sobre bairros, equipe para rankings de equipes e corretor para rankings de corretores.",
        },
        corretor: {
          type: "string",
          description:
            "Nome completo ou parte inequívoca do nome do corretor. Use junto com agrupar_por=bairro para descobrir em quais bairros ele vendeu.",
        },
        equipe: {
          type: "string",
          description:
            "Nome completo ou parte inequívoca da equipe. Use junto com agrupar_por=equipe para uma avaliação comparativa da equipe.",
        },
        top_n: {
          type: "integer",
          minimum: 1,
          maximum: 50,
          default: 10,
          description:
            "Quantidade máxima de posições retornadas. Use 3 para Top 3; o padrão é 10.",
        },
      },
      required: ["data_inicio", "data_fim"],
    },
    outputSchema: {
      type: "object",
      properties: {
        contract_version: { type: "string" },
        official_source: { type: "string" },
        commercial_source: { type: "string" },
        attribution: { type: "string" },
        group_by: { type: "string" },
        broker_filter: { type: ["string", "null"] },
        matched_brokers: { type: "array", items: { type: "string" } },
        metric: { type: "string" },
        period: { type: "object" },
        coverage: { type: "object" },
        generated_at: { type: "string" },
        summary: { type: "object" },
        ranking: { type: "array" },
        visualization: { type: "object" },
      },
      required: [
        "contract_version",
        "official_source",
        "commercial_source",
        "attribution",
        "group_by",
        "metric",
        "period",
        "coverage",
        "generated_at",
        "summary",
        "ranking",
        "visualization",
      ],
    },
  },
  {
    name: "consultar_funil_vista",
    title: "Consultar negócios criados e etapa atual no Vista",
    description:
      "Consulta negócios distintos cadastrados no Vista dentro de um período inclusivo, sem filtrar o status geral, e os agrupa por status, etapa atual e matriz etapa por status. Também separa os negócios atualmente em Proposta por equipe quando agrupar_por=equipe. Use para volume de negócios criados, fotografia atual da coorte e quantidade atualmente aberta em cada etapa. Não use a quantidade atualmente em Proposta como se fosse o total de propostas geradas no período: essa última métrica exige um histórico de entrada em etapas ainda não disponível na integração Vista.",
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        data_inicio: {
          type: "string",
          format: "date",
          description: "Data inicial inclusiva de cadastro do negócio, no formato YYYY-MM-DD.",
        },
        data_fim: {
          type: "string",
          format: "date",
          description: "Data final inclusiva de cadastro do negócio, no formato YYYY-MM-DD.",
        },
        agrupar_por: {
          type: "string",
          enum: ["nenhum", "equipe"],
          default: "nenhum",
          description:
            "Use equipe para separar a fotografia atual dos negócios em Proposta por equipe.",
        },
      },
      required: ["data_inicio", "data_fim"],
    },
    outputSchema: {
      type: "object",
      properties: {
        contract_version: { type: "string" },
        source: { type: "string" },
        group_by: { type: "string" },
        period: { type: "object" },
        coverage: { type: "object" },
        generated_at: { type: "string" },
        semantics: { type: "object" },
        summary: { type: "object" },
        supported_questions: { type: "array", items: { type: "string" } },
        unsupported_without_stage_history: {
          type: "array",
          items: { type: "string" },
        },
        response_guidance: { type: "object" },
        visualization: { type: ["object", "null"] },
      },
      required: [
        "contract_version",
        "source",
        "group_by",
        "period",
        "coverage",
        "generated_at",
        "semantics",
        "summary",
        "supported_questions",
        "unsupported_without_stage_history",
        "response_guidance",
        "visualization",
      ],
    },
  },
];

Deno.serve(async (request: Request) => {
  const url = new URL(request.url);

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204 });
  }

  if (
    url.pathname.endsWith("/.well-known/oauth-protected-resource") ||
    url.pathname.endsWith("/mcp/.well-known/oauth-protected-resource")
  ) {
    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    if (!supabaseUrl) return json({ error: "server_configuration_error" }, 500);
    return json({
      resource: resourceUrl(url),
      authorization_servers: [supabaseUrl + "/auth/v1"],
      scopes_supported: ["openid", "email", "profile"],
      bearer_methods_supported: ["header"],
      resource_name: SERVER_NAME,
    });
  }

  const normalizedPath = url.pathname.replace(/\/+$/, "");
  const canonicalSuffix = "/" + FUNCTION_SLUG + "/mcp";
  if (normalizedPath !== "/mcp" && !normalizedPath.endsWith(canonicalSuffix)) {
    return json(
      {
        error: "not_found",
        mcp_endpoint: resourceUrl(url),
      },
      404,
    );
  }

  const auth = await authorize(request);
  if (!auth.ok) {
    if (auth.reason === "server_configuration_error") {
      return json({ error: auth.reason }, 500);
    }
    if (auth.reason === "access_denied") {
      return json({ error: auth.reason }, 403);
    }
    return unauthorized(url);
  }

  if (request.method === "GET") {
    return json({
      name: SERVER_NAME,
      version: SERVER_VERSION,
      protocol: "mcp-streamable-http",
      authenticated: true,
    });
  }
  if (request.method !== "POST") {
    return json({ error: "method_not_allowed" }, 405);
  }

  let message: JsonRpcRequest;
  try {
    message = await request.json();
  } catch {
    return rpcError(null, -32700, "Parse error");
  }
  if (message.jsonrpc !== "2.0" || !message.method) {
    return rpcError(message.id, -32600, "Invalid Request");
  }

  if (message.method.startsWith("notifications/")) {
    return new Response(null, { status: 202 });
  }
  if (message.method === "ping") {
    return rpcResult(message.id, {});
  }
  if (message.method === "initialize") {
    const requested = String(message.params?.protocolVersion ?? "");
    const supported = new Set(["2025-06-18", "2025-03-26", "2024-11-05"]);
    return rpcResult(message.id, {
      protocolVersion: supported.has(requested) ? requested : "2025-03-26",
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
      instructions:
        "Use consultar_ranking_vendas somente para vendas oficiais. Use consultar_funil_vista para negócios cadastrados no período, status geral, etapa atual e cruzamento entre etapa e status. Perguntas sobre propostas ou outras etapas, inclusive pedidos de separação por equipe, pertencem sempre a consultar_funil_vista; envie agrupar_por=equipe quando a equipe for solicitada. Para rankings de vendas por equipes, use consultar_ranking_vendas com agrupar_por=equipe; para avaliar uma equipe de vendas específica, informe também equipe. Para saber o bairro em que um corretor mais vendeu, use agrupar_por=bairro e informe corretor. Use top_n conforme solicitado, com padrão 10. Quantidade é o critério padrão; VGV deve ser solicitado explicitamente. O fim de períodos futuros é limitado automaticamente à data atual de São Paulo. Quantidade, data e VGV vêm das APIs ao vivo; a planilha é somente uma referência gerencial de responsável para equipe com vigência. Responda primeiro com o número, ranking ou conclusão solicitada, sem bordão ou prefixo padronizado. Perguntas objetivas devem receber uma ou duas frases; acrescente período, cobertura e fonte somente quando forem necessários para evitar interpretação errada ou quando o usuário pedir. Em avaliações gerenciais, apresente fatos, comparação, leitura executiva e ação recomendada apenas quando os dados sustentarem essas conclusões. No funil, diferencie obrigatoriamente negócios criados no período, etapa atual, status geral e eventos históricos de entrada em etapa. Nunca apresente negócios atualmente em Proposta como propostas geradas no período; esta métrica exige histórico de etapas. Se pedirem uma métrica histórica indisponível, apresente primeiro a fotografia atual verificada em no máximo 80 palavras e esclareça a diferença em uma frase. Não repita a pergunta, não liste fontes, timestamps ou limitações técnicas salvo se forem solicitados e nunca diga que existe confirmação de contrato pendente. Se a ferramenta retornar erro, informe a falha em uma frase curta; não transforme valores ausentes em análise. Não conclua sobre conversão de pipeline, visitas ou tempo entre etapas sem os dados operacionais correspondentes. A visualização é fornecida como dados estruturados e nunca deve ser substituída por barras ASCII ou código Python.",
    });
  }
  if (message.method === "tools/list") {
    return rpcResult(message.id, { tools: TOOLS });
  }
  if (message.method === "tools/call") {
    const name = message.params?.name;
    const args =
      message.params?.arguments && typeof message.params.arguments === "object"
        ? (message.params.arguments as Record<string, unknown>)
        : {};
    if (
      name !== "consultar_ranking_vendas" &&
      name !== "consultar_funil_vista"
    ) {
      return rpcError(message.id, -32602, "Unknown tool");
    }
    const result = name === "consultar_funil_vista"
      ? await callVistaFunnelCohort(auth.token, args, auth.userClient)
      : await callSalesRanking(auth.token, args, auth.userClient);
    return rpcResult(message.id, {
      content: textContent(result.value),
      structuredContent: result.value,
      isError: result.isError,
    });
  }

  return rpcError(message.id, -32601, "Method not found");
});
