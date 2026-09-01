import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2.57.4";
import {
  buildBrokerTeamIndex,
  buildManagerTeamIndex,
  resolveBrokerTeam,
  resolveManagerTeam,
  type BrokerTeamReference,
  type ManagerTeamReference,
} from "./team_reference.ts";

const FUNCTION_SLUG = "gralha-indicadores-mcp";
const SERVER_NAME = "Gralha — Indicadores Pipeimob × Vista";
const SERVER_VERSION = "1.15.0";
const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};
const EXECUTIVE_ROLES = new Set(["ceo", "cso", "cmo"]);
const ACCESS_ROLES = new Set([
  "ceo",
  "cso",
  "cmo",
  "store_director",
  "team_manager",
]);
const CIRCUIT_FAILURE_THRESHOLD = 2;
const CIRCUIT_WINDOW_MS = 120_000;
const CIRCUIT_COOLDOWN_MS = 60_000;

type IntegrationOperation =
  | "sales_reconciliation"
  | "sales_neighborhood_detail"
  | "vista_funnel_cohort";

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

  const [{ data: profile, error: profileError }, { data: teamRows, error: teamError }] =
    await Promise.all([
      userClient
        .from("profiles")
        .select("status,access_role")
        .eq("id", user.id)
        .maybeSingle(),
      userClient
        .from("user_team_access")
        .select("team_key")
        .eq("user_id", user.id),
    ]);
  if (profileError || teamError) {
    console.error("authorization_lookup_failed", {
      profileCode: profileError?.code ?? null,
      teamCode: teamError?.code ?? null,
    });
    return { ok: false as const, reason: "authorization_lookup_failed" };
  }
  const role = typeof profile?.access_role === "string"
    ? profile.access_role
    : "";
  if (profile?.status !== "active" || !ACCESS_ROLES.has(role)) {
    return { ok: false as const, reason: "access_denied" };
  }
  const allowedTeamKeys = [...new Set((teamRows ?? []).map((row) =>
    normalizeBrokerName(String(row.team_key ?? ""))
  ).filter(Boolean))];
  const hasGlobalAccess = EXECUTIVE_ROLES.has(role);
  if (
    (!hasGlobalAccess && allowedTeamKeys.length === 0) ||
    (role === "team_manager" && allowedTeamKeys.length !== 1)
  ) {
    return { ok: false as const, reason: "invalid_team_scope" };
  }

  return {
    ok: true as const,
    token,
    userId: user.id,
    role,
    userClient,
    hasGlobalAccess,
    allowedTeamKeys,
  };
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
): Promise<{
  reachable: boolean;
  status: number;
  payload: unknown;
  integrationError: string | null;
}> {
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
    return {
      reachable: true,
      status: response.status,
      payload,
      integrationError:
        response.headers.get("X-Funnel-Error") ??
        response.headers.get("X-Reconciliation-Error"),
    };
  } catch {
    return {
      reachable: false,
      status: 0,
      payload: null,
      integrationError: null,
    };
  }
}

function safeDiagnosticCode(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const normalized = value.trim().toLocaleLowerCase("en-US");
  return /^[a-z0-9][a-z0-9_:-]{0,79}$/.test(normalized)
    ? normalized
    : fallback;
}

async function recordIntegrationFailure(
  userClient: ReturnType<typeof createClient>,
  input: {
    operation: IntegrationOperation;
    periodStart: string;
    periodEnd: string;
    httpStatus: number;
    errorCode: string;
  },
): Promise<void> {
  try {
    const { error } = await userClient.from("integration_failure_diagnostics")
      .insert({
        function_slug: FUNCTION_SLUG,
        function_version: SERVER_VERSION,
        operation: input.operation,
        period_start: input.periodStart,
        period_end: input.periodEnd,
        http_status: input.httpStatus,
        error_code: safeDiagnosticCode(input.errorCode, "unclassified"),
      });
    if (error) {
      console.warn("integration_failure_diagnostic_insert_failed", {
        code: error.code,
      });
    }
  } catch {
    console.warn("integration_failure_diagnostic_insert_failed", {
      code: "unexpected",
    });
  }
}

async function readIntegrationCircuit(
  userClient: ReturnType<typeof createClient>,
  operation: IntegrationOperation,
): Promise<{
  verified: boolean;
  open: boolean;
  retryAfterSeconds: number;
  errorCode: string | null;
  httpStatus: number | null;
}> {
  const since = new Date(Date.now() - CIRCUIT_WINDOW_MS).toISOString();
  const { data, error } = await userClient
    .from("integration_failure_diagnostics")
    .select("occurred_at,http_status,error_code")
    .eq("operation", operation)
    .gte("occurred_at", since)
    .order("occurred_at", { ascending: false })
    .limit(CIRCUIT_FAILURE_THRESHOLD);
  if (error) {
    console.warn("integration_circuit_lookup_failed", {
      operation,
      code: error.code,
    });
    return {
      verified: false,
      open: false,
      retryAfterSeconds: 0,
      errorCode: null,
      httpStatus: null,
    };
  }

  const failures = data ?? [];
  const latest = failures[0];
  if (failures.length < CIRCUIT_FAILURE_THRESHOLD || !latest?.occurred_at) {
    return {
      verified: true,
      open: false,
      retryAfterSeconds: 0,
      errorCode: null,
      httpStatus: null,
    };
  }
  const latestAt = Date.parse(String(latest.occurred_at));
  const retryAfterSeconds = Number.isFinite(latestAt)
    ? Math.max(
      0,
      Math.ceil((latestAt + CIRCUIT_COOLDOWN_MS - Date.now()) / 1000),
    )
    : 0;
  return {
    verified: true,
    open: retryAfterSeconds > 0,
    retryAfterSeconds,
    errorCode: safeDiagnosticCode(latest.error_code, "unclassified"),
    httpStatus: asNumber(latest.http_status),
  };
}

async function sourceAvailability(
  userClient: ReturnType<typeof createClient>,
): Promise<{ isError: false; value: unknown }> {
  const [sales, salesNeighborhood, vista] = await Promise.all([
    readIntegrationCircuit(userClient, "sales_reconciliation"),
    readIntegrationCircuit(userClient, "sales_neighborhood_detail"),
    readIntegrationCircuit(userClient, "vista_funnel_cohort"),
  ]);
  return {
    isError: false,
    value: {
      checked_at: new Date().toISOString(),
      sources: {
        sales: {
          verified: sales.verified,
          available: sales.verified && !sales.open,
          retry_after_seconds: sales.retryAfterSeconds,
          error_code: sales.open ? sales.errorCode : null,
        },
        sales_neighborhood_detail: {
          verified: salesNeighborhood.verified,
          available: salesNeighborhood.verified && !salesNeighborhood.open,
          retry_after_seconds: salesNeighborhood.retryAfterSeconds,
          error_code: salesNeighborhood.open
            ? salesNeighborhood.errorCode
            : null,
        },
        vista_funnel: {
          verified: vista.verified,
          available: vista.verified && !vista.open,
          retry_after_seconds: vista.retryAfterSeconds,
          error_code: vista.open ? vista.errorCode : null,
        },
      },
    },
  };
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

  const salesCircuit = await readIntegrationCircuit(
    userClient,
    "sales_reconciliation",
  );
  if (salesCircuit.open) {
    return {
      isError: true,
      value: {
        error: "source_circuit_open",
        source: "sales",
        retry_after_seconds: salesCircuit.retryAfterSeconds,
        detail:
          "A fonte de vendas apresentou falhas recorrentes e está em verificação temporária.",
      },
    };
  }

  const reconciliation = await fetchBackendJson(reconciliationEndpoint, token);
  if (!reconciliation.reachable) {
    EdgeRuntime.waitUntil(
      recordIntegrationFailure(userClient, {
        operation: "sales_reconciliation",
        periodStart: start,
        periodEnd: end,
        httpStatus: 0,
        errorCode: "backend_unreachable",
      }),
    );
    return {
      isError: true,
      value: {
        error: "backend_unreachable",
        detail: "O serviço de conciliação não respondeu no tempo esperado.",
      },
    };
  }
  if (reconciliation.status < 200 || reconciliation.status >= 300) {
    const upstream = reconciliation.payload &&
        typeof reconciliation.payload === "object"
      ? reconciliation.payload as Record<string, unknown>
      : {};
    const upstreamErrorCode = safeDiagnosticCode(
      reconciliation.integrationError ?? upstream.error_code,
      reconciliation.status === 401
        ? "backend_auth_rejected"
        : "upstream_http_error",
    );
    EdgeRuntime.waitUntil(
      recordIntegrationFailure(userClient, {
        operation: "sales_reconciliation",
        periodStart: start,
        periodEnd: end,
        httpStatus: reconciliation.status,
        errorCode: upstreamErrorCode,
      }),
    );
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
    EdgeRuntime.waitUntil(
      recordIntegrationFailure(userClient, {
        operation: "sales_reconciliation",
        periodStart: start,
        periodEnd: end,
        httpStatus: reconciliation.status,
        errorCode: "invalid_upstream_contract",
      }),
    );
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
    const neighborhoodCircuit = await readIntegrationCircuit(
      userClient,
      "sales_neighborhood_detail",
    );
    if (neighborhoodCircuit.open) {
      return {
        isError: true,
        value: {
          error: "source_circuit_open",
          source: "sales_neighborhood_detail",
          retry_after_seconds: neighborhoodCircuit.retryAfterSeconds,
          detail:
            "O detalhamento de bairros apresentou falhas recorrentes e está em verificação temporária.",
        },
      };
    }
    const transactionsResult = await fetchBackendJson(
      transactionsEndpoint,
      token,
    );
    if (!transactionsResult.reachable) {
      EdgeRuntime.waitUntil(
        recordIntegrationFailure(userClient, {
          operation: "sales_neighborhood_detail",
          periodStart: start,
          periodEnd: end,
          httpStatus: 0,
          errorCode: "backend_unreachable",
        }),
      );
      return {
        isError: true,
        value: {
          error: "backend_unreachable",
          detail: "O detalhamento de bairros não respondeu no tempo esperado.",
        },
      };
    }
    if (transactionsResult.status < 200 || transactionsResult.status >= 300) {
      const upstream = transactionsResult.payload &&
          typeof transactionsResult.payload === "object"
        ? transactionsResult.payload as Record<string, unknown>
        : {};
      const upstreamErrorCode = safeDiagnosticCode(
        transactionsResult.integrationError ?? upstream.error_code,
        transactionsResult.status === 401
          ? "backend_auth_rejected"
          : "upstream_http_error",
      );
      EdgeRuntime.waitUntil(
        recordIntegrationFailure(userClient, {
          operation: "sales_neighborhood_detail",
          periodStart: start,
          periodEnd: end,
          httpStatus: transactionsResult.status,
          errorCode: upstreamErrorCode,
        }),
      );
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
  const teamFilter =
    typeof args.equipe === "string" && args.equipe.trim()
      ? args.equipe.replace(/\s+/g, " ").trim()
      : null;
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

  const vistaCircuit = await readIntegrationCircuit(
    userClient,
    "vista_funnel_cohort",
  );
  if (vistaCircuit.open) {
    return {
      isError: true,
      value: {
        error: "source_circuit_open",
        source: "vista_funnel",
        retry_after_seconds: vistaCircuit.retryAfterSeconds,
        detail:
          "A API do Vista apresentou falhas recorrentes e está em verificação temporária.",
      },
    };
  }

  const result = await fetchBackendJson(endpoint, token);
  if (!result.reachable) {
    EdgeRuntime.waitUntil(
      recordIntegrationFailure(userClient, {
        operation: "vista_funnel_cohort",
        periodStart: start,
        periodEnd: end,
        httpStatus: 0,
        errorCode: "backend_unreachable",
      }),
    );
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
    const upstreamErrorCode = safeDiagnosticCode(
      result.integrationError ?? upstream.error_code,
      result.status === 401 ? "backend_auth_rejected" : "upstream_http_error",
    );
    console.warn("vista_funnel_backend_failed", {
      status: result.status,
      errorCode: upstreamErrorCode,
    });
    EdgeRuntime.waitUntil(
      recordIntegrationFailure(userClient, {
        operation: "vista_funnel_cohort",
        periodStart: start,
        periodEnd: end,
        httpStatus: result.status,
        errorCode: upstreamErrorCode,
      }),
    );
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
    EdgeRuntime.waitUntil(
      recordIntegrationFailure(userClient, {
        operation: "vista_funnel_cohort",
        periodStart: start,
        periodEnd: end,
        httpStatus: result.status,
        errorCode: "invalid_upstream_contract",
      }),
    );
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
  const stageAssignmentBreakdown = Array.isArray(
      summary.stage_assignment_breakdown,
    )
    ? summary.stage_assignment_breakdown
    : [];
  const {
    stage_assignment_breakdown: _privateStageAssignmentBreakdown,
    ...safeSummary
  } = summary;
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
  let teamFunnelBreakdown: Array<Record<string, unknown>> = [];
  let teamFunnelCoverage: Record<string, unknown> | null = null;
  let selectedTeamFunnel: Record<string, unknown> | null = null;
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

    const { data: brokerReferenceRows, error: brokerReferenceError } =
      await userClient
        .from("sales_team_reference")
        .select(
          "broker_key,broker_name,team_key,team_name,sale_date,source_updated_through",
        )
        .lte("sale_date", end)
        .order("broker_key", { ascending: true })
        .order("sale_date", { ascending: true })
        .limit(5000);

    if (referenceError || brokerReferenceError) {
      console.error("proposal_team_reference_lookup_failed", {
        managerCode: referenceError?.code ?? null,
        brokerCode: brokerReferenceError?.code ?? null,
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
    const brokerReferences = (brokerReferenceRows ?? []) as BrokerTeamReference[];
    const historyByBroker = buildBrokerTeamIndex(brokerReferences);
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
    let brokerReferenceOpen = 0;
    let ambiguousBrokerReferenceOpen = 0;
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

    const funnelGrouped = new Map<
      string,
      {
        team: string;
        deals_count: number;
        stages: Map<string, { stage: string; deals_count: number }>;
        statuses: Map<string, { status: string; deals_count: number }>;
      }
    >();
    let assignedDeals = 0;
    let apiTeamDeals = 0;
    let brokerReferenceDeals = 0;
    let ambiguousBrokerReferenceDeals = 0;
    let managerReferenceDeals = 0;
    let managerReferenceReviewDeals = 0;
    let ambiguousDeals = 0;
    let unresolvedDeals = 0;

    for (const candidate of stageAssignmentBreakdown) {
      if (!candidate || typeof candidate !== "object") continue;
      const row = candidate as Record<string, unknown>;
      const dealsCount = Math.max(0, asNumber(row.deals_count) ?? 0);
      if (!dealsCount) continue;
      const stageName =
        typeof row.stage === "string" && row.stage.trim()
          ? row.stage.replace(/\s+/g, " ").trim()
          : "Etapa não identificada";
      const statusName =
        typeof row.status === "string" && row.status.trim()
          ? row.status.replace(/\s+/g, " ").trim()
          : "Status não identificado";
      let teamName =
        typeof row.team === "string"
          ? row.team.replace(/\s+/g, " ").trim()
          : "";
      let teamKey = teamName ? normalizeBrokerName(teamName) : "";

      if (teamName) {
        apiTeamDeals += dealsCount;
      } else {
        const responsible =
          typeof row.responsible === "string"
            ? row.responsible.replace(/\s+/g, " ").trim()
            : "";
        const createdDate =
          typeof row.created_date === "string"
            ? row.created_date.slice(0, 10)
            : "";
        const brokerAssignment = resolveBrokerTeam(
          historyByBroker,
          responsible,
          createdDate,
        );
        if (brokerAssignment.status === "resolved") {
          teamName = brokerAssignment.teamName;
          teamKey = brokerAssignment.teamKey;
          brokerReferenceDeals += dealsCount;
        } else if (brokerAssignment.status === "ambiguous") {
          ambiguousBrokerReferenceDeals += dealsCount;
        } else {
          const managerAssignment = resolveManagerTeam(
            historyByManager,
            responsible,
            createdDate,
          );
          if (managerAssignment.status === "resolved") {
            teamName = managerAssignment.teamName;
            teamKey = managerAssignment.teamKey;
            managerReferenceDeals += dealsCount;
            if (managerAssignment.reviewRequired) {
              managerReferenceReviewDeals += dealsCount;
            }
          } else if (managerAssignment.status === "ambiguous") {
            ambiguousDeals += dealsCount;
          } else {
            unresolvedDeals += dealsCount;
          }
        }
      }

      if (!teamName || !teamKey) continue;
      assignedDeals += dealsCount;
      const groupedRow = funnelGrouped.get(teamKey) ?? {
        team: teamName,
        deals_count: 0,
        stages: new Map(),
        statuses: new Map(),
      };
      groupedRow.deals_count += dealsCount;
      const stageKey = normalizeBrokerName(stageName);
      const stageRow = groupedRow.stages.get(stageKey) ?? {
        stage: stageName,
        deals_count: 0,
      };
      stageRow.deals_count += dealsCount;
      groupedRow.stages.set(stageKey, stageRow);
      const statusKey = normalizeBrokerName(statusName);
      const statusRow = groupedRow.statuses.get(statusKey) ?? {
        status: statusName,
        deals_count: 0,
      };
      statusRow.deals_count += dealsCount;
      groupedRow.statuses.set(statusKey, statusRow);
      funnelGrouped.set(teamKey, groupedRow);
    }

    teamFunnelBreakdown = [...funnelGrouped.values()]
      .map((row) => ({
        team: row.team,
        deals_count: row.deals_count,
        stage_breakdown: [...row.stages.values()].sort((a, b) =>
          b.deals_count - a.deals_count ||
          a.stage.localeCompare(b.stage, "pt-BR")
        ),
        status_breakdown: [...row.statuses.values()].sort((a, b) =>
          b.deals_count - a.deals_count ||
          a.status.localeCompare(b.status, "pt-BR")
        ),
      }))
      .sort((a, b) =>
        Number(b.deals_count) - Number(a.deals_count) ||
        String(a.team).localeCompare(String(b.team), "pt-BR")
      );
    const totalCreatedDeals = Math.max(
      0,
      asNumber(summary.created_deals) ?? 0,
    );
    teamFunnelCoverage = {
      created_deals_total: totalCreatedDeals,
      assigned_deals: assignedDeals,
      unassigned_deals: Math.max(0, totalCreatedDeals - assignedDeals),
      assignment_rate: totalCreatedDeals ? assignedDeals / totalCreatedDeals : 0,
      api_team_deals: apiTeamDeals,
      broker_reference_deals: brokerReferenceDeals,
      ambiguous_broker_reference_deals: ambiguousBrokerReferenceDeals,
      manager_reference_deals: managerReferenceDeals,
      manager_reference_review_deals: managerReferenceReviewDeals,
      ambiguous_manager_reference_deals: ambiguousDeals,
      unresolved_deals: unresolvedDeals,
      reference_updated_through: sourceUpdatedThrough,
    };
    const normalizedTeamFilter = teamFilter
      ? normalizeBrokerName(teamFilter)
      : null;
    selectedTeamFunnel = normalizedTeamFilter
      ? teamFunnelBreakdown.find((row) => {
          const normalized = normalizeBrokerName(String(row.team));
          return normalized === normalizedTeamFilter ||
            normalized.includes(normalizedTeamFilter);
        }) ?? null
      : null;
    if (normalizedTeamFilter && !selectedTeamFunnel) {
      return {
        isError: true,
        value: {
          error: "team_not_found",
          detail:
            "Nenhuma equipe atribuída corresponde ao nome informado no período.",
          team_filter: teamFilter,
          available_teams: teamFunnelBreakdown.map((row) => row.team),
          period: root.period ?? { start, end },
        },
      };
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
        const brokerAssignment = resolveBrokerTeam(
          historyByBroker,
          responsible,
          createdDate,
        );
        if (brokerAssignment.status === "resolved") {
          teamName = brokerAssignment.teamName;
          teamKey = brokerAssignment.teamKey;
          brokerReferenceOpen += openCount;
        } else if (brokerAssignment.status === "ambiguous") {
          ambiguousBrokerReferenceOpen += openCount;
        } else {
          const managerAssignment = resolveManagerTeam(
            historyByManager,
            responsible,
            createdDate,
          );
          if (managerAssignment.status === "resolved") {
            teamName = managerAssignment.teamName;
            teamKey = managerAssignment.teamKey;
            managerReferenceOpen += openCount;
            if (managerAssignment.reviewRequired) {
              managerReferenceReviewOpen += openCount;
            }
          } else if (managerAssignment.status === "ambiguous") {
            ambiguousOpen += openCount;
          } else {
            unresolvedOpen += openCount;
          }
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
      broker_reference_open: brokerReferenceOpen,
      ambiguous_broker_reference_open: ambiguousBrokerReferenceOpen,
      manager_reference_open: managerReferenceOpen,
      manager_reference_review_open: managerReferenceReviewOpen,
      ambiguous_manager_reference_open: ambiguousOpen,
      unresolved_open: unresolvedOpen,
      reference_updated_through: sourceUpdatedThrough,
    };
    visualization = selectedTeamFunnel
      ? {
        type: "funnel",
        title: `Funil comercial — ${String(selectedTeamFunnel.team)}`,
        metric: "deals_count",
        unit: "deals",
        series: Array.isArray(selectedTeamFunnel.stage_breakdown)
          ? selectedTeamFunnel.stage_breakdown.map((row) => ({
            label: String((row as Record<string, unknown>).stage ?? ""),
            value: asNumber(
              (row as Record<string, unknown>).deals_count,
            ) ?? 0,
          }))
          : [],
        footnote:
          "Fotografia atual dos negócios criados no período e atribuídos à equipe.",
      }
      : {
        type: "bar",
        title: "Negócios do funil por equipe",
        metric: "deals_count",
        unit: "deals",
        series: teamFunnelBreakdown.slice(0, 10).map((row) => ({
          label: String(row.team),
          value: asNumber(row.deals_count) ?? 0,
        })),
        footnote:
          `${assignedDeals} de ${totalCreatedDeals} negócios possuem equipe atribuída. ` +
          "Fotografia atual dos negócios criados no período.",
      };
  }

  return {
    isError: false,
    value: {
      contract_version: "1.3",
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
        ...safeSummary,
        ...(groupBy === "equipe"
          ? {
            team_funnel: {
              team_breakdown: teamFunnelBreakdown,
              team_coverage: teamFunnelCoverage,
              selected_team: selectedTeamFunnel,
              team_filter: teamFilter,
            },
          }
          : {}),
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
        "como o funil atual se distribui por equipe",
        "quantos negócios de uma etapa atual pertencem a uma equipe específica",
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
    name: "verificar_disponibilidade_fontes",
    title: "Verificar disponibilidade das fontes de indicadores",
    description:
      "Consulta somente a telemetria técnica sanitizada recente de Pipeimob/Vista. Use antes de uma análise generativa quando houver falhas recorrentes, evitando novas consultas e consumo de tokens enquanto a fonte estiver em recuperação.",
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: false,
    },
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {},
    },
    outputSchema: {
      type: "object",
      properties: {
        checked_at: { type: "string" },
        sources: { type: "object" },
      },
      required: ["checked_at", "sources"],
    },
  },
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
      "Consulta negócios distintos cadastrados no Vista dentro de um período inclusivo, sem filtrar o status geral, e os agrupa por status, etapa atual e matriz etapa por status. Quando agrupar_por=equipe, separa todas as etapas atuais por equipe e aceita o filtro opcional equipe. Use para volume de negócios criados, fotografia atual da coorte e quantidade atualmente aberta em cada etapa. Não use a quantidade atualmente em Proposta como se fosse o total de propostas geradas no período: essa última métrica exige um histórico de entrada em etapas ainda não disponível na integração Vista.",
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
            "Use equipe para separar todas as etapas atuais do funil por equipe.",
        },
        equipe: {
          type: "string",
          minLength: 1,
          maxLength: 120,
          description:
            "Nome completo ou parte inequívoca da equipe. Use com agrupar_por=equipe para consultar o funil de uma equipe específica.",
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

type AuthorizedContext = Extract<Awaited<ReturnType<typeof authorize>>, { ok: true }>;

function teamAllowed(value: unknown, allowedTeamKeys: string[]): boolean {
  return allowedTeamKeys.includes(normalizeBrokerName(String(value ?? "")));
}

function sumRows(rows: unknown[], field: string): number {
  return rows.reduce((total, row) => {
    if (!row || typeof row !== "object") return total;
    return total + Math.max(0, asNumber((row as Record<string, unknown>)[field]) ?? 0);
  }, 0);
}

function scopeToolResult(value: unknown, auth: AuthorizedContext): unknown {
  if (auth.hasGlobalAccess || !value || typeof value !== "object") return value;
  const root = structuredClone(value as Record<string, unknown>);
  const allowed = auth.allowedTeamKeys;

  if (root.group_by === "team" && Array.isArray(root.ranking)) {
    const ranking = root.ranking.filter((row) =>
      row && typeof row === "object" &&
      teamAllowed((row as Record<string, unknown>).team, allowed)
    );
    root.ranking = ranking.map((row, index) => ({
      ...(row as Record<string, unknown>),
      position: index + 1,
    }));
    root.total_ranked = ranking.length;
    root.team_filter = null;
    root.team_evaluation = null;
    root.summary = {
      scoped_sales: sumRows(ranking, "sales_count"),
      scoped_vgv: sumRows(ranking, "vgv"),
      scope: "authorized_teams_only",
    };
    const visualization = root.visualization as Record<string, unknown> | null;
    if (visualization && Array.isArray(visualization.series)) {
      visualization.series = visualization.series.filter((row) =>
        row && typeof row === "object" &&
        teamAllowed((row as Record<string, unknown>).label, allowed)
      );
      visualization.footnote = "Exibição restrita às equipes autorizadas para este usuário.";
    }
    return root;
  }

  const summary = root.summary && typeof root.summary === "object"
    ? root.summary as Record<string, unknown>
    : null;
  const teamFunnel = summary?.team_funnel && typeof summary.team_funnel === "object"
    ? summary.team_funnel as Record<string, unknown>
    : null;
  if (root.group_by === "equipe" && summary && teamFunnel) {
    const teamRows = (Array.isArray(teamFunnel.team_breakdown)
      ? teamFunnel.team_breakdown
      : []).filter((row) =>
        row && typeof row === "object" &&
        teamAllowed((row as Record<string, unknown>).team, allowed)
      );
    const scopedDeals = sumRows(teamRows, "deals_count");
    teamFunnel.team_breakdown = teamRows;
    teamFunnel.selected_team = teamRows.length === 1 ? teamRows[0] : null;
    teamFunnel.team_filter = null;
    teamFunnel.team_coverage = {
      created_deals_total: scopedDeals,
      assigned_deals: scopedDeals,
      unassigned_deals: 0,
      assignment_rate: scopedDeals > 0 ? 1 : 0,
      scope: "authorized_teams_only",
    };

    const proposal = summary.proposal && typeof summary.proposal === "object"
      ? summary.proposal as Record<string, unknown>
      : null;
    if (proposal) {
      const proposalRows = (Array.isArray(proposal.team_breakdown)
        ? proposal.team_breakdown
        : []).filter((row) =>
          row && typeof row === "object" &&
          teamAllowed((row as Record<string, unknown>).team, allowed)
        );
      const scopedOpen = sumRows(proposalRows, "open_deals_count");
      const scopedCurrent = sumRows(proposalRows, "current_stage_deals_count");
      proposal.team_breakdown = proposalRows;
      proposal.created_deals_in_proposal_stage_with_open_status = scopedOpen;
      proposal.created_deals_currently_in_proposal = scopedCurrent;
      proposal.team_coverage = {
        proposal_open_total: scopedOpen,
        proposal_current_stage_total: scopedCurrent,
        assigned_open: scopedOpen,
        assigned_current_stage: scopedCurrent,
        scope: "authorized_teams_only",
      };
    }
    root.visualization = {
      type: "multi_funnel",
      title: "Funil comercial — equipes autorizadas",
      metric: "deals_count",
      unit: "deals",
      groups: teamRows.map((row) => {
        const record = row as Record<string, unknown>;
        return {
          label: String(record.team ?? "Equipe"),
          series: (Array.isArray(record.stage_breakdown)
            ? record.stage_breakdown
            : []).map((stage) => ({
              label: String((stage as Record<string, unknown>).stage ?? ""),
              value: asNumber((stage as Record<string, unknown>).deals_count) ?? 0,
            })),
        };
      }),
      footnote: "Exibição restrita às equipes autorizadas para este usuário.",
    };
  }
  return root;
}

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
        "Use verificar_disponibilidade_fontes apenas como verificação técnica antes de uma análise generativa quando houver sinais de falhas recorrentes; se a fonte necessária estiver bloqueada, não execute outra consulta nem produza análise. Use consultar_ranking_vendas somente para vendas oficiais. Use consultar_funil_vista para negócios cadastrados no período, status geral, etapa atual e cruzamento entre etapa e status. Perguntas sobre visitas, agendamentos, propostas ou outras etapas, inclusive pedidos de separação por equipe, pertencem sempre a consultar_funil_vista; envie agrupar_por=equipe e equipe=<nome> quando uma equipe específica for solicitada. Para rankings de vendas por equipes, use consultar_ranking_vendas com agrupar_por=equipe; para avaliar uma equipe de vendas específica, informe também equipe. Para saber o bairro em que um corretor mais vendeu, use agrupar_por=bairro e informe corretor. Use top_n conforme solicitado, com padrão 10. Quantidade é o critério padrão; VGV deve ser solicitado explicitamente. O fim de períodos futuros é limitado automaticamente à data atual de São Paulo. Quantidade, data e VGV vêm das APIs ao vivo; a planilha é somente uma referência gerencial de responsável para equipe com vigência. Responda primeiro com o número, ranking ou conclusão solicitada, sem bordão ou prefixo padronizado. Perguntas objetivas devem receber uma ou duas frases; acrescente período, cobertura e fonte somente quando forem necessários para evitar interpretação errada ou quando o usuário pedir. Em avaliações gerenciais, apresente fatos, comparação, leitura executiva e ação recomendada apenas quando os dados sustentarem essas conclusões. No funil, diferencie obrigatoriamente negócios criados no período, etapa atual, status geral e eventos históricos de entrada em etapa. Uma contagem na etapa Visita representa negócios atualmente nessa etapa, não visitas realizadas. Nunca apresente negócios atualmente em Proposta como propostas geradas no período; esta métrica exige um histórico de entrada em etapas. Se pedirem uma métrica histórica indisponível, apresente primeiro a fotografia atual verificada em no máximo 80 palavras e esclareça a diferença em uma frase. Não repita a pergunta, não liste fontes, timestamps ou limitações técnicas salvo se forem solicitados e nunca diga que existe confirmação de contrato pendente. Se a ferramenta retornar erro, informe a falha em uma frase curta; não transforme valores ausentes em análise. Não conclua sobre conversão de pipeline, eventos históricos de visitas ou tempo entre etapas sem os dados operacionais correspondentes. A visualização é fornecida como dados estruturados e nunca deve ser substituída por barras ASCII ou código Python.",
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
      name !== "verificar_disponibilidade_fontes" &&
      name !== "consultar_ranking_vendas" &&
      name !== "consultar_funil_vista"
    ) {
      return rpcError(message.id, -32602, "Unknown tool");
    }
    const scopedArgs = auth.hasGlobalAccess || name === "verificar_disponibilidade_fontes"
      ? args
      : {
        ...args,
        agrupar_por: "equipe",
        top_n: 50,
        equipe: undefined,
      };
    const result = name === "verificar_disponibilidade_fontes"
      ? await sourceAvailability(auth.userClient)
      : name === "consultar_funil_vista"
        ? await callVistaFunnelCohort(auth.token, scopedArgs, auth.userClient)
        : await callSalesRanking(auth.token, scopedArgs, auth.userClient);
    const scopedValue = result.isError
      ? result.value
      : scopeToolResult(result.value, auth);
    return rpcResult(message.id, {
      content: textContent(scopedValue),
      structuredContent: scopedValue,
      isError: result.isError,
    });
  }

  return rpcError(message.id, -32601, "Method not found");
});
