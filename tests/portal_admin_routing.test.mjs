import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const adminSource = await readFile(
  new URL("../supabase/functions/gralha-portal-admin/index.ts", import.meta.url),
  "utf8",
);
const configToml = await readFile(
  new URL("../supabase/config.toml", import.meta.url),
  "utf8",
);

// Replicate extractSuffix exact implementation from gralha-portal-admin/index.ts for executable validation
const FUNCTION_SLUG = "gralha-portal-admin";
function extractSuffix(pathname) {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  const stripped = normalized.replace(
    new RegExp(`^(?:/functions/v1)?/${FUNCTION_SLUG}(?=/|$)`),
    "",
  );
  return stripped || "/";
}

test("normalizes all three variations of /me route to /me", () => {
  const path1 = "/functions/v1/gralha-portal-admin/me";
  const path2 = "/gralha-portal-admin/me";
  const path3 = "/me";

  assert.equal(extractSuffix(path1), "/me");
  assert.equal(extractSuffix(path2), "/me");
  assert.equal(extractSuffix(path3), "/me");
});

test("normalizes all variations of teams, users, and user-id routes", () => {
  // /teams variations
  assert.equal(extractSuffix("/functions/v1/gralha-portal-admin/teams"), "/teams");
  assert.equal(extractSuffix("/gralha-portal-admin/teams"), "/teams");
  assert.equal(extractSuffix("/teams"), "/teams");

  // /users variations
  assert.equal(extractSuffix("/functions/v1/gralha-portal-admin/users"), "/users");
  assert.equal(extractSuffix("/gralha-portal-admin/users"), "/users");
  assert.equal(extractSuffix("/users"), "/users");

  // /users/:id variations
  const userId = "c18f152d-8b83-4a1e-a590-a7ecfb0a8b91";
  assert.equal(
    extractSuffix(`/functions/v1/gralha-portal-admin/users/${userId}`),
    `/users/${userId}`,
  );
  assert.equal(
    extractSuffix(`/gralha-portal-admin/users/${userId}`),
    `/users/${userId}`,
  );
  assert.equal(
    extractSuffix(`/users/${userId}`),
    `/users/${userId}`,
  );
});

test("normalizes root and unknown routes properly without crashing", () => {
  assert.equal(extractSuffix("/functions/v1/gralha-portal-admin"), "/");
  assert.equal(extractSuffix("/gralha-portal-admin"), "/");
  assert.equal(extractSuffix("/"), "/");

  assert.equal(extractSuffix("/functions/v1/gralha-portal-admin/unknown-path"), "/unknown-path");
  assert.equal(extractSuffix("/gralha-portal-admin/unknown-path"), "/unknown-path");
  assert.equal(extractSuffix("/unknown-path"), "/unknown-path");
});

test("preserves fallback 404 for unmatched routes in router", () => {
  assert.match(
    adminSource,
    /return json\(\{ error: "not_found" \}, 404\);/,
  );
  assert.match(
    adminSource,
    /const suffix = extractSuffix\(url\.pathname\);/,
  );
});

test("preserves 401 authentication enforcement in authorize function", () => {
  assert.match(
    adminSource,
    /if \(!token \|\| !supabaseUrl \|\| !publishableKey \|\| !serviceRoleKey\) \{\s*return \{ ok: false as const, status: 401, error: "authentication_required" \};\s*\}/,
  );
  assert.match(
    adminSource,
    /if \(userError \|\| !user\) \{\s*return \{ ok: false as const, status: 401, error: "invalid_session" \};\s*\}/,
  );
});

test("preserves fail-closed RBAC access roles and scope checks", () => {
  assert.match(
    adminSource,
    /const EXECUTIVE_ROLES = new Set\(\["ceo", "cso", "cmo"\]\);/,
  );
  assert.match(
    adminSource,
    /const ACCESS_ROLES = new Set\(\[\s*"ceo",\s*"cso",\s*"cmo",\s*"store_director",\s*"team_manager",?\s*\]\);/,
  );
  assert.match(
    adminSource,
    /if \(!ACCESS_ROLES\.has\(profile\.access_role\)\) \{\s*return \{ ok: false as const, status: 403, error: "invalid_access_role" \};\s*\}/,
  );
  assert.match(
    adminSource,
    /if \(profileError \|\| !profile \|\| profile\.status !== "active"\) \{\s*return \{ ok: false as const, status: 403, error: "access_denied" \};\s*\}/,
  );
});

test("configures gralha-portal-admin and gralha-indicadores-mcp in config.toml", () => {
  assert.match(
    configToml,
    /\[functions\.gralha-indicadores-mcp\]\s*verify_jwt = false/,
  );
  assert.match(
    configToml,
    /\[functions\.gralha-portal-admin\]\s*verify_jwt = false/,
  );
});
