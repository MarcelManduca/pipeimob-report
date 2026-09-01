import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "npm:@supabase/supabase-js@2.57.4";

const FUNCTION_SLUG = "gralha-portal-admin";
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

type AccessRole =
  | "ceo"
  | "cso"
  | "cmo"
  | "store_director"
  | "team_manager";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: JSON_HEADERS,
  });
}

function bearer(request: Request): string {
  const header = request.headers.get("Authorization") ?? "";
  return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
}

function cleanText(value: unknown, maxLength: number): string {
  return typeof value === "string"
    ? value.replace(/\s+/g, " ").trim().slice(0, maxLength)
    : "";
}

function validEmail(value: unknown): string {
  const email = cleanText(value, 254).toLocaleLowerCase("pt-BR");
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ? email : "";
}

function roleLabel(role: string): string {
  return {
    ceo: "CEO",
    cso: "CSO",
    cmo: "CMO",
    store_director: "Diretor de loja",
    team_manager: "Gerente de equipe",
  }[role] ?? role;
}

function normalizeTeamKeys(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.flatMap((item) => {
    const key = cleanText(item, 160).toLocaleLowerCase("pt-BR");
    return key ? [key] : [];
  }))].slice(0, 50);
}

function validateRoleScope(role: AccessRole, teamKeys: string[]): string | null {
  if (EXECUTIVE_ROLES.has(role) && teamKeys.length) {
    return "Cargos executivos possuem acesso geral e não devem receber equipes específicas.";
  }
  if (role === "store_director" && teamKeys.length < 1) {
    return "Selecione pelo menos uma equipe para o Diretor de loja.";
  }
  if (role === "team_manager" && teamKeys.length !== 1) {
    return "O Gerente de equipe deve possuir exatamente uma equipe.";
  }
  return null;
}

function inviteRedirect(value: unknown): string | null {
  try {
    const candidate = new URL(String(value ?? ""));
    if (candidate.protocol !== "https:" || candidate.username || candidate.password) {
      return null;
    }
    return candidate.origin + "/reset-password";
  } catch {
    return null;
  }
}

async function authorize(request: Request) {
  const token = bearer(request);
  const supabaseUrl = Deno.env.get("SUPABASE_URL") ?? "";
  const publishableKey = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
  if (!token || !supabaseUrl || !publishableKey || !serviceRoleKey) {
    return { ok: false as const, status: 401, error: "authentication_required" };
  }

  const userClient = createClient(supabaseUrl, publishableKey, {
    global: { headers: { Authorization: `Bearer ${token}` } },
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const adminClient = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data: userData, error: userError } = await userClient.auth.getUser(token);
  const user = userData.user;
  if (userError || !user) {
    return { ok: false as const, status: 401, error: "invalid_session" };
  }

  const { data: profile, error: profileError } = await adminClient
    .from("profiles")
    .select("id,email,display_name,status,access_role")
    .eq("id", user.id)
    .maybeSingle();
  if (profileError || !profile || profile.status !== "active") {
    return { ok: false as const, status: 403, error: "access_denied" };
  }
  if (!ACCESS_ROLES.has(profile.access_role)) {
    return { ok: false as const, status: 403, error: "invalid_access_role" };
  }

  const { data: teamAccess, error: teamError } = await adminClient
    .from("user_team_access")
    .select("team_key,teams(team_name)")
    .eq("user_id", user.id)
    .order("team_key");
  if (teamError) {
    return { ok: false as const, status: 500, error: "team_scope_unavailable" };
  }
  const teams = (teamAccess ?? []).map((row) => ({
    team_key: String(row.team_key),
    team_name: String(
      Array.isArray(row.teams)
        ? row.teams[0]?.team_name ?? row.team_key
        : (row.teams as { team_name?: string } | null)?.team_name ?? row.team_key,
    ),
  }));

  return {
    ok: true as const,
    user,
    profile: {
      ...profile,
      access_role: profile.access_role as AccessRole,
      role_label: roleLabel(profile.access_role),
      teams,
      has_global_access: EXECUTIVE_ROLES.has(profile.access_role),
      can_manage_users: EXECUTIVE_ROLES.has(profile.access_role),
    },
    adminClient,
  };
}

async function parseBody(request: Request): Promise<Record<string, unknown> | null> {
  const length = Number(request.headers.get("content-length") ?? "0");
  if (length > 32_000) return null;
  try {
    const value = await request.json();
    return value && typeof value === "object"
      ? value as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

async function validateTeamKeys(
  adminClient: ReturnType<typeof createClient>,
  teamKeys: string[],
): Promise<boolean> {
  if (!teamKeys.length) return true;
  const { data, error } = await adminClient
    .from("teams")
    .select("team_key")
    .eq("is_active", true)
    .in("team_key", teamKeys);
  return !error && new Set((data ?? []).map((row) => row.team_key)).size === teamKeys.length;
}

async function listUsers(auth: Extract<Awaited<ReturnType<typeof authorize>>, { ok: true }>) {
  if (!auth.profile.can_manage_users) return json({ error: "access_denied" }, 403);
  const [{ data: profiles, error: profilesError }, { data: access, error: accessError }] =
    await Promise.all([
      auth.adminClient
        .from("profiles")
        .select("id,email,display_name,status,access_role,created_at,activated_at,disabled_at")
        .order("created_at", { ascending: true }),
      auth.adminClient
        .from("user_team_access")
        .select("user_id,team_key,teams(team_name)")
        .order("team_key", { ascending: true }),
    ]);
  if (profilesError || accessError) {
    return json({ error: "user_directory_unavailable" }, 500);
  }
  const accessByUser = new Map<string, Array<{ team_key: string; team_name: string }>>();
  for (const row of access ?? []) {
    const current = accessByUser.get(row.user_id) ?? [];
    current.push({
      team_key: row.team_key,
      team_name: String(
        Array.isArray(row.teams)
          ? row.teams[0]?.team_name ?? row.team_key
          : (row.teams as { team_name?: string } | null)?.team_name ?? row.team_key,
      ),
    });
    accessByUser.set(row.user_id, current);
  }
  return json({
    users: (profiles ?? []).map((profile) => ({
      ...profile,
      role_label: roleLabel(profile.access_role),
      teams: accessByUser.get(profile.id) ?? [],
    })),
  });
}

async function listTeams(auth: Extract<Awaited<ReturnType<typeof authorize>>, { ok: true }>) {
  const { data, error } = await auth.adminClient
    .from("teams")
    .select("team_key,team_name,is_active")
    .eq("is_active", true)
    .order("team_name", { ascending: true });
  if (error) return json({ error: "team_directory_unavailable" }, 500);
  const allowed = auth.profile.has_global_access
    ? data ?? []
    : (data ?? []).filter((team) =>
      auth.profile.teams.some((allowedTeam) => allowedTeam.team_key === team.team_key)
    );
  return json({ teams: allowed });
}

async function replaceTeamAccess(
  auth: Extract<Awaited<ReturnType<typeof authorize>>, { ok: true }>,
  userId: string,
  teamKeys: string[],
) {
  const { error: deleteError } = await auth.adminClient
    .from("user_team_access")
    .delete()
    .eq("user_id", userId);
  if (deleteError) return deleteError;
  if (!teamKeys.length) return null;
  const { error } = await auth.adminClient.from("user_team_access").insert(
    teamKeys.map((teamKey) => ({
      user_id: userId,
      team_key: teamKey,
      created_by: auth.user.id,
    })),
  );
  return error;
}

async function inviteUser(
  request: Request,
  auth: Extract<Awaited<ReturnType<typeof authorize>>, { ok: true }>,
) {
  if (!auth.profile.can_manage_users) return json({ error: "access_denied" }, 403);
  const body = await parseBody(request);
  if (!body) return json({ error: "invalid_payload" }, 400);
  const email = validEmail(body.email);
  const displayName = cleanText(body.display_name, 120);
  const role = cleanText(body.access_role, 40) as AccessRole;
  const teamKeys = normalizeTeamKeys(body.team_keys);
  const redirectTo = inviteRedirect(body.portal_origin);
  if (!email || !displayName || !ACCESS_ROLES.has(role) || !redirectTo) {
    return json({ error: "invalid_user_data" }, 400);
  }
  const scopeError = validateRoleScope(role, teamKeys);
  if (scopeError) return json({ error: scopeError }, 400);
  if (!(await validateTeamKeys(auth.adminClient, teamKeys))) {
    return json({ error: "Uma ou mais equipes informadas não são válidas." }, 400);
  }

  const { data: existing } = await auth.adminClient
    .from("profiles")
    .select("id")
    .ilike("email", email)
    .maybeSingle();
  if (existing) return json({ error: "Este e-mail já possui um usuário." }, 409);

  const { data: invitation, error: inviteError } =
    await auth.adminClient.auth.admin.inviteUserByEmail(email, {
      data: { display_name: displayName },
      redirectTo,
    });
  const invitedUser = invitation.user;
  if (inviteError || !invitedUser) {
    console.error("portal_invite_failed", { code: inviteError?.code ?? null });
    return json({ error: "Não foi possível enviar o convite agora." }, 502);
  }

  const { error: profileError } = await auth.adminClient.from("profiles").upsert({
    id: invitedUser.id,
    email,
    display_name: displayName,
    access_role: role,
    status: "invited",
    status_updated_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }, { onConflict: "id" });
  const { error: legacyRoleError } = await auth.adminClient.from("user_roles").upsert({
    user_id: invitedUser.id,
    role: "viewer",
  }, { onConflict: "user_id" });
  const accessError = await replaceTeamAccess(auth, invitedUser.id, teamKeys);
  if (profileError || legacyRoleError || accessError) {
    await auth.adminClient.auth.admin.deleteUser(invitedUser.id);
    console.error("portal_invite_profile_failed", {
      profileCode: profileError?.code ?? null,
      roleCode: legacyRoleError?.code ?? null,
      accessCode: accessError?.code ?? null,
    });
    return json({ error: "O convite não pôde ser concluído com segurança." }, 500);
  }

  await auth.adminClient.from("user_management_audit").insert({
    actor_user_id: auth.user.id,
    target_user_id: invitedUser.id,
    action: "user_invited",
    details: { access_role: role, team_keys: teamKeys },
  });
  return json({
    user: {
      id: invitedUser.id,
      email,
      display_name: displayName,
      access_role: role,
      role_label: roleLabel(role),
      status: "invited",
      team_keys: teamKeys,
    },
  }, 201);
}

async function updateUser(
  request: Request,
  auth: Extract<Awaited<ReturnType<typeof authorize>>, { ok: true }>,
  userId: string,
) {
  if (!auth.profile.can_manage_users) return json({ error: "access_denied" }, 403);
  const body = await parseBody(request);
  if (!body) return json({ error: "invalid_payload" }, 400);
  const displayName = cleanText(body.display_name, 120);
  const role = cleanText(body.access_role, 40) as AccessRole;
  const status = cleanText(body.status, 20);
  const teamKeys = normalizeTeamKeys(body.team_keys);
  if (!displayName || !ACCESS_ROLES.has(role) || !["active", "disabled", "invited"].includes(status)) {
    return json({ error: "invalid_user_data" }, 400);
  }
  if (userId === auth.user.id && status === "disabled") {
    return json({ error: "Você não pode desativar o próprio acesso." }, 400);
  }
  if (userId === auth.user.id && !EXECUTIVE_ROLES.has(role)) {
    return json({ error: "Você não pode remover o próprio acesso executivo." }, 400);
  }
  const scopeError = validateRoleScope(role, teamKeys);
  if (scopeError) return json({ error: scopeError }, 400);
  if (!(await validateTeamKeys(auth.adminClient, teamKeys))) {
    return json({ error: "Uma ou mais equipes informadas não são válidas." }, 400);
  }
  const now = new Date().toISOString();
  const { data: updated, error: updateError } = await auth.adminClient
    .from("profiles")
    .update({
      display_name: displayName,
      access_role: role,
      status,
      status_updated_at: now,
      updated_at: now,
      activated_at: status === "active" ? now : null,
      disabled_at: status === "disabled" ? now : null,
    })
    .eq("id", userId)
    .select("id,email,display_name,status,access_role")
    .maybeSingle();
  if (updateError || !updated) return json({ error: "Usuário não encontrado." }, 404);
  const accessError = await replaceTeamAccess(auth, userId, teamKeys);
  if (accessError) return json({ error: "Não foi possível atualizar as equipes." }, 500);
  await auth.adminClient.from("user_management_audit").insert({
    actor_user_id: auth.user.id,
    target_user_id: userId,
    action: "user_updated",
    details: { access_role: role, status, team_keys: teamKeys },
  });
  return json({ user: { ...updated, role_label: roleLabel(role), team_keys: teamKeys } });
}

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") return new Response(null, { status: 204 });
  const url = new URL(request.url);
  const auth = await authorize(request);
  if (!auth.ok) return json({ error: auth.error }, auth.status);

  const suffix = url.pathname.split(`/functions/v1/${FUNCTION_SLUG}`).pop() || "/";
  if (request.method === "GET" && suffix === "/me") return json({ profile: auth.profile });
  if (request.method === "GET" && suffix === "/teams") return listTeams(auth);
  if (request.method === "GET" && suffix === "/users") return listUsers(auth);
  if (request.method === "POST" && suffix === "/users") return inviteUser(request, auth);
  const userMatch = suffix.match(/^\/users\/([0-9a-f-]{36})$/i);
  if (request.method === "PATCH" && userMatch) {
    return updateUser(request, auth, userMatch[1]);
  }
  return json({ error: "not_found" }, 404);
});
