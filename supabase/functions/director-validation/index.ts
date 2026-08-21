import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2";

const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function isAuthorizedDirector(email: string): boolean {
  const normalized = email.trim().toLowerCase();
  const allowedEmails = new Set(
    (Deno.env.get("MCP_ALLOWED_DIRECTOR_EMAILS") ?? "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
  const allowedDomains = new Set(
    (Deno.env.get("MCP_ALLOWED_DIRECTOR_DOMAINS") ?? "gralhaimoveis.com.br")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
  const domain = normalized.includes("@") ? normalized.split("@").at(-1) ?? "" : "";
  return allowedEmails.has(normalized) || allowedDomains.has(domain);
}

function validDate(value: string | null): value is string {
  return Boolean(value && /^\d{4}-\d{2}-\d{2}$/.test(value));
}

Deno.serve(async (request) => {
  if (request.method !== "GET" && request.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed" }, 405);
  }

  const authorization = request.headers.get("Authorization") ?? "";
  const token = authorization.startsWith("Bearer ")
    ? authorization.slice("Bearer ".length).trim()
    : "";
  if (!token) {
    return jsonResponse({ error: "authentication_required" }, 401);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return jsonResponse({ error: "server_configuration_error" }, 500);
  }

  const supabase = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data: userData, error: userError } = await supabase.auth.getUser(token);
  const email = userData.user?.email ?? "";
  if (userError || !email || !isAuthorizedDirector(email)) {
    return jsonResponse({ error: "access_denied" }, 403);
  }

  const url = new URL(request.url);
  const start = url.searchParams.get("data_inicio_ccv") ?? url.searchParams.get("data_inicio");
  const end = url.searchParams.get("data_fim_ccv") ?? url.searchParams.get("data_fim");
  if (!validDate(start) || !validDate(end)) {
    return jsonResponse({ error: "invalid_period" }, 400);
  }

  const startDate = new Date(`${start}T00:00:00Z`);
  const endDate = new Date(`${end}T00:00:00Z`);
  const days = (endDate.getTime() - startDate.getTime()) / 86_400_000;
  if (!Number.isFinite(days) || days < 0 || days > 366) {
    return jsonResponse({ error: "invalid_period" }, 400);
  }

  const { data, error } = await supabase.rpc(
    "get_validation_sales_reconciliation",
    { p_start: start, p_end: end },
  );
  if (error) {
    return jsonResponse({ error: "validation_data_unavailable" }, 503);
  }

  return jsonResponse(data);
});
