import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2.57.4";
import {
  buildManagerTeamIndex,
  resolveManagerTeam,
  type ManagerTeamReference,
} from "./team_reference.ts";

const FUNCTION_SLUG = "gralha-indicadores-mcp";
const SERVER_NAME = "Gralha — Indicadores Pipeimob × Vista";
const SERVER_VERSION = "1.6.0";
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

function json(body: unknown, status = 200, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...headers },
  });
}

function functionBaseUrl(url: URL): string {
  const configured = Deno.env.get("SUPABASE_URL");
  const origin = configured ? new URL(configured).origin : "https://" + url.host;
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
  return json(
    { error: "authentication_required" },
    401,
    { "WWW-Authenticate": 'Bearer resource_metadata="' + metadata + '"' },
  );
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
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(parsed);
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

async function authorize(request: Request) {
  const header = request.headers.get("Authorization") ?? "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  if (!token) return { ok: false as const, reason: "missing_token" };

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  let publishableKey = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
  const publishableKeysJson = Deno.env.get("SUPABASE_PUBLISHABLE_KEYS");
  if (publishableKeysJson) {
    try {
      const publishableKeys = JSON.parse(publishableKeysJson) as Record<string, unknown>;
      const selected =
        (typeof publishableKeys.default === "string" && publishableKeys.default) ||
        Object.values(publishableKeys).find((value) => typeof value === "string");
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
  const { data: userData, error: userError } = await userClient.auth.getUser(token);
  const user = userData.user;
  if (userError || !user) return { ok: false as const, reason: "invalid_token" };

  const [
    { data: profile, error: profileError },
    { data: roles, error: rolesError },
  ] = await Promise.all([
    userClient.from("profiles").select("status").eq("id", user.id).maybeSingle(),
    userClient.from("user_roles").select("role").eq("user_id", user.id),
  ]);
  if (profileError || rolesError) {
    console.error("authorization_lookup_failed", {
      profileCode: profileError?.code ?? null,
      rolesCode: rolesError?.code ?? null,
    });
    return { ok: false as const, reason: "authorization_lookup_failed" };
  }

  const role = roles?.find((candidate) =>
    typeof candidate.role === "string" && ALLOWED_ROLES.has(candidate.role)
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
  return String(value ?? "").replace(/\s+/g, "").trim().toLocaleUpperCase("pt-BR");
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
      signal: AbortSignal.timeout(55_000),
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
  const start = args.data_inicio ?? args.data_inicio_ccv;
  const end = args.data_fim ?? args.data_fim_ccv;
  const validationError = validatePeriod(start, end);
  if (validationError) {
    return {
      isError: true,
      value: { error: "invalid_period", detail: validationError },
    };
  }

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
  const requestedGroup = String(args.agrupar_por ?? "corretor");
  const groupBy = requestedGroup === "equipe"
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
      console.error("team_reference_lookup_failed", { code: referenceError.code });
      return {
        isError: true,
        value: {
          error: "team_reference_unavailable",
          detail: "O vínculo gerencial entre responsáveis e equipes não pôde ser consultado.",
        },
      };
    }

    const references = (referenceRows ?? []) as ManagerTeamReference[];
    const historyByManager = buildManagerTeamIndex(references);
    let sourceUpdatedThrough: string | null = null;

    for (const reference of references) {
      const updatedThrough = String(reference.source_updated_through ?? "").slice(0, 10);
      if (updatedThrough && (!sourceUpdatedThrough || updatedThrough > sourceUpdatedThrough)) {
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
        return b.vgv - a.vgv || b.sales_count - a.sales_count ||
          a.team.localeCompare(b.team, "pt-BR");
      }
      return b.sales_count - a.sales_count || b.vgv - a.vgv ||
        a.team.localeCompare(b.team, "pt-BR");
    });

    return {
      isError: false,
      value: {
        contract_version: "2.0",
        official_source: root.official_source ?? "pipeimob_api_v2",
        commercial_source: root.commercial_source ?? "vista_negocio_ganho",
        team_reference_source: "management_spreadsheet_manager_team_dimension",
        attribution: "api_team_then_date_aware_manager_reference",
        group_by: "team",
        metric,
        period: root.period ?? { start, end },
        generated_at: root.generated_at ?? new Date().toISOString(),
        source_updated_through: sourceUpdatedThrough,
        summary: {
          attributed_sales: attributedSales,
          attributed_vgv: attributedVgv,
          assigned_sales: assignedSales,
          assigned_vgv: assignedVgv,
          sales_without_team: Math.max(attributedSales - assignedSales, 0),
          team_assignment_rate: attributedSales > 0 ? assignedSales / attributedSales : 0,
          api_team_sales: apiTeamSales,
          api_team_conflict_sales: apiTeamConflictSales,
          manager_reference_sales: managerReferenceSales,
          manager_reference_review_sales: managerReferenceReviewSales,
          ambiguous_manager_reference_sales: ambiguousManagerReferenceSales,
        },
        ranking: ranking.map((row, index) => ({
          position: index + 1,
          team: row.team,
          sales_count: row.sales_count,
          vgv: row.vgv,
          average_ticket: row.sales_count > 0 ? row.vgv / row.sales_count : 0,
        })),
      },
    };
  }

  if (groupBy === "bairro") {
    const transactionsEndpoint = new URL(backend + "/api/transactions");
    transactionsEndpoint.searchParams.set("data_inicio_ccv", String(start));
    transactionsEndpoint.searchParams.set("data_fim_ccv", String(end));
    const transactionsResult = await fetchBackendJson(transactionsEndpoint, token);
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
      transactionsResult.payload && typeof transactionsResult.payload === "object"
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
          detail: "Nenhuma venda atribuída foi encontrada para o corretor informado no período.",
          broker_filter: brokerFilter,
          period: root.period ?? { start, end },
        },
      };
    }

    const ranking = [...grouped.values()];
    ranking.sort((a, b) => {
      if (metric === "vgv") {
        return b.vgv - a.vgv || b.sales_count - a.sales_count ||
          a.neighborhood.localeCompare(b.neighborhood, "pt-BR");
      }
      return b.sales_count - a.sales_count || b.vgv - a.vgv ||
        a.neighborhood.localeCompare(b.neighborhood, "pt-BR");
    });

    return {
      isError: false,
      value: {
        contract_version: "1.2",
        official_source: root.official_source ?? "pipeimob_api_v2",
        commercial_source: root.commercial_source ?? "vista_negocio_ganho",
        attribution: "vista_commercial_broker",
        group_by: "neighborhood",
        broker_filter: brokerFilter,
        matched_brokers: [...matchedBrokers].sort((a, b) =>
          a.localeCompare(b, "pt-BR")
        ),
        metric,
        period: root.period ?? { start, end },
        generated_at: root.generated_at ?? new Date().toISOString(),
        summary: {
          attributed_sales: attributedSales,
          attributed_vgv: attributedVgv,
          sales_with_neighborhood: attributedSales - missingNeighborhoodSales,
          sales_without_neighborhood: missingNeighborhoodSales,
        },
        ranking: ranking.map((row, index) => ({
          position: index + 1,
          neighborhood: row.neighborhood,
          sales_count: row.sales_count,
          vgv: row.vgv,
          average_ticket: row.sales_count > 0 ? row.vgv / row.sales_count : 0,
        })),
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
      return b.vgv - a.vgv || b.sales_count - a.sales_count ||
        a.commercial_broker.localeCompare(b.commercial_broker, "pt-BR");
    }
    return b.sales_count - a.sales_count || b.vgv - a.vgv ||
      a.commercial_broker.localeCompare(b.commercial_broker, "pt-BR");
  });

  const officialSales = asNumber(summary.official_sales) ?? officialIds.size;
  const officialVgv = asNumber(summary.official_vgv) ?? derivedOfficialVgv;
  return {
    isError: false,
    value: {
      contract_version: "1.1",
      official_source: root.official_source ?? "pipeimob_api_v2",
      commercial_source: root.commercial_source ?? "vista_negocio_ganho",
      attribution: "vista_commercial_broker",
      group_by: "commercial_broker",
      metric,
      period: root.period ?? { start, end },
      generated_at: root.generated_at ?? new Date().toISOString(),
      summary: {
        official_sales: officialSales,
        official_vgv: officialVgv,
        attributed_sales: attributedIds.size,
        attributed_vgv: attributedVgv,
        unattributed_sales: Math.max(officialSales - attributedIds.size, 0),
        unattributed_vgv: Math.max(officialVgv - attributedVgv, 0),
      },
      ranking: ranking.map((row, index) => ({
        position: index + 1,
        commercial_broker: row.commercial_broker,
        sales_count: row.sales_count,
        vgv: row.vgv,
        average_ticket: row.sales_count > 0 ? row.vgv / row.sales_count : 0,
      })),
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
        generated_at: { type: "string" },
        summary: { type: "object" },
        ranking: { type: "array" },
      },
      required: [
        "contract_version",
        "official_source",
        "commercial_source",
        "attribution",
        "group_by",
        "metric",
        "period",
        "generated_at",
        "summary",
        "ranking",
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
    return json({
      error: "not_found",
      mcp_endpoint: resourceUrl(url),
    }, 404);
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
        "Use consultar_ranking_vendas para perguntas sobre vendas. Para rankings de equipes, use agrupar_por=equipe. Para saber o bairro em que um corretor mais vendeu, use agrupar_por=bairro e informe corretor. Quantidade é o critério padrão; VGV deve ser solicitado explicitamente. Quantidade, data e VGV vêm das APIs ao vivo; a planilha é somente uma referência gerencial de responsável para equipe com vigência. Sempre informe o período, a cobertura da referência e vendas sem atribuição.",
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
    if (name !== "consultar_ranking_vendas") {
      return rpcError(message.id, -32602, "Unknown tool");
    }
    const result = await callSalesRanking(auth.token, args, auth.userClient);
    return rpcResult(message.id, {
      content: textContent(result.value),
      structuredContent: result.value,
      isError: result.isError,
    });
  }

  return rpcError(message.id, -32601, "Method not found");
});
