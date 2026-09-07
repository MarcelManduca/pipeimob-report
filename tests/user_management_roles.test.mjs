import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workerSource = await readFile(
  new URL("../cloudflare/gralha-indicadores-chat-worker-v12.js", import.meta.url),
  "utf8",
);
const adminSource = await readFile(
  new URL("../supabase/functions/gralha-portal-admin/index.ts", import.meta.url),
  "utf8",
);

test("CSS: .team-select does not use display:grid!important and allows .hidden override", () => {
  assert.match(
    workerSource,
    /\.team-select\{grid-column:1\/-1;display:grid;grid-template-columns:repeat\(3,minmax\(0,1fr\)\);gap:8px\}/,
  );
  assert.doesNotMatch(
    workerSource,
    /\.team-select\{[^}]*display:grid!important/,
  );
  assert.match(
    workerSource,
    /\.hidden\{display:none!important\}/,
  );
  assert.match(
    workerSource,
    /\.team-global-note\{grid-column:1\/-1;padding:12px 14px;/,
  );
  assert.match(
    workerSource,
    /\.team-global-badge\{display:inline-block;padding:6px 10px;/,
  );
});

test("Invite form: shows global team note and hides individual team checkboxes for CEO, CSO, CMO", () => {
  assert.match(
    workerSource,
    /<div id="invite-teams-global" class="team-global-note">Todas as equipes — acesso global<\/div>/,
  );
  assert.match(
    workerSource,
    /const role=\$\("invite-role"\)\.value,isExec=\["ceo","cso","cmo"\]\.includes\(role\)/,
  );
  assert.match(
    workerSource,
    /\$\("invite-teams-global"\)\.classList\.toggle\("hidden",!isExec\)/,
  );
  assert.match(
    workerSource,
    /\$\("invite-teams"\)\.classList\.toggle\("hidden",!needsTeam\)/,
  );
  assert.match(
    workerSource,
    /team_keys=isExec\?\[\]:selectedTeams\(\$\("invite-teams"\)\)/,
  );
});

test("Invite form: enforces scope validation for store_director and team_manager", () => {
  assert.match(
    workerSource,
    /if\(role==="store_director"&&team_keys\.length<1\)\{showError\(\$\("admin-error"\),"Selecione pelo menos uma equipe para o Diretor de loja\."\);return\}/,
  );
  assert.match(
    workerSource,
    /if\(role==="team_manager"&&team_keys\.length!==1\)\{showError\(\$\("admin-error"\),"O Gerente de equipe deve possuir exatamente uma equipe\."\);return\}/,
  );
});

test("Users table: renders global team badge and toggles team multi-select on role changes", () => {
  assert.match(
    workerSource,
    /globalLabel\.className="team-global-badge"/,
  );
  assert.match(
    workerSource,
    /globalLabel\.textContent="Todas as equipes"/,
  );
  assert.match(
    workerSource,
    /const updateRowScope=\(\)=>\{const isExec=\["ceo","cso","cmo"\]\.includes\(role\.value\);teams\.classList\.toggle\("hidden",isExec\);globalLabel\.classList\.toggle\("hidden",!isExec\);if\(isExec\)\[\.\.\.teams\.options\]\.forEach\(opt=>opt\.selected=false\)\};/,
  );
  assert.match(
    workerSource,
    /role\.addEventListener\("change",updateRowScope\)/,
  );
});

test("Users table: enforces validation and sends empty team_keys for executive roles upon save", () => {
  assert.match(
    workerSource,
    /const isExec=\["ceo","cso","cmo"\]\.includes\(role\.value\),\s*team_keys=isExec\?\[\]:\[\.\.\.teams\.selectedOptions\]\.map\(option=>option\.value\)/,
  );
  assert.match(
    workerSource,
    /if\(role\.value==="store_director"&&team_keys\.length<1\)\{saveButton\.disabled=false;showError\(\$\("admin-error"\),"Selecione pelo menos uma equipe para o Diretor de loja\."\);return\}/,
  );
  assert.match(
    workerSource,
    /if\(role\.value==="team_manager"&&team_keys\.length!==1\)\{saveButton\.disabled=false;showError\(\$\("admin-error"\),"O Gerente de equipe deve possuir exatamente uma equipe\."\);return\}/,
  );
});

test("Backend: preserves fail-closed RBAC scope validation across all 5 roles", () => {
  assert.match(
    adminSource,
    /function validateRoleScope\(role: AccessRole, teamKeys: string\[\]\): string \| null \{\s*if \(EXECUTIVE_ROLES\.has\(role\) && teamKeys\.length\) \{\s*return "Cargos executivos possuem acesso geral e não devem receber equipes específicas\.";\s*\}\s*if \(role === "store_director" && teamKeys\.length < 1\) \{\s*return "Selecione pelo menos uma equipe para o Diretor de loja\.";\s*\}\s*if \(role === "team_manager" && teamKeys\.length !== 1\) \{\s*return "O Gerente de equipe deve possuir exatamente uma equipe\.";\s*\}\s*return null;\s*\}/,
  );
});
