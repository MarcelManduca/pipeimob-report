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

test("Invite flow: button has loading state 'Enviando convite...' and is disabled during in-flight request", () => {
  assert.match(
    workerSource,
    /let inviteInFlight=false;/,
    "Invite submission must have an in-flight boolean lock",
  );
  assert.match(
    workerSource,
    /if\(inviteInFlight\)return;/,
    "Invite submission must ignore duplicate triggers while in flight",
  );
  assert.match(
    workerSource,
    /inviteInFlight=true;\s*button\.disabled=true;\s*button\.textContent="Enviando convite\.\.\.";/,
    "Button must be disabled immediately with 'Enviando convite...' text",
  );
  assert.match(
    workerSource,
    /button\.disabled=false;\s*button\.textContent="Convidar usuário";/,
    "Button state and text must be restored upon completion or error",
  );
});

test("Invite flow: displays accessible success confirmation with aria-live and recipient email", () => {
  assert.match(
    workerSource,
    /<p id="admin-success" class="admin-feedback success hidden" role="status" aria-live="polite"><\/p>/,
    "admin-success element must have role='status' and aria-live='polite'",
  );
  assert.match(
    workerSource,
    /showError\(\$\("admin-success"\),"Convite enviado para "\+inviteEmail\+"\."\);/,
    "Success confirmation message must include invited email",
  );
  assert.match(
    workerSource,
    /event\.currentTarget\.reset\(\);\s*updateInviteScope\(\);/,
    "Form must be cleared only after success",
  );
});

test("Invite flow: handles pending invite (409) with dedicated friendly Portuguese message", () => {
  assert.match(
    adminSource,
    /const isPending = existing\.status === "invited";\s*return json\({\s*error: isPending\s*\?\s*"Já existe um convite pendente para este e-mail\."\s*:\s*"Este e-mail já possui um usuário cadastrado\.",\s*code: isPending \? "invite_pending" : "user_exists",\s*}, 409\);/,
    "Backend must return specific 409 message when an invite is pending",
  );
  assert.match(
    workerSource,
    /if\(res\.status===409\)\{\s*showError\(\$\("admin-error"\),data\.error\|\|"Já existe um convite pendente para este e-mail\."\)/,
    "Frontend must display friendly message on 409 status",
  );
});

test("Users table: defaults to read-only view mode with 'Editar' button and highlights current user with 'Você'", () => {
  assert.match(
    workerSource,
    /const youBadge=document\.createElement\("span"\);\s*youBadge\.className="you-badge";\s*youBadge\.textContent="Você";/,
    "Current user row must display '(Você)' badge",
  );
  assert.match(
    workerSource,
    /const editBtn=document\.createElement\("button"\);\s*editBtn\.className="nav-button edit-btn";\s*editBtn\.type="button";\s*editBtn\.textContent="Editar";\s*editBtn\.addEventListener\("click",\(\)=>renderEditRow\(\)\);/,
    "View mode row must have an 'Editar' button that triggers edit mode",
  );
  assert.match(
    workerSource,
    /const statusBadge=document\.createElement\("span"\);\s*statusBadge\.className="status-chip status-"\+\(user\.status\|\|"active"\);/,
    "View mode row must display status badge chip",
  );
});

test("Users table: edit mode allows editing name, role, scope and status while email is strictly read-only", () => {
  assert.match(
    workerSource,
    /nameInput\.className="edit-input";\s*nameInput\.type="text";\s*nameInput\.value=user\.display_name\|\|"";\s*nameInput\.placeholder="Nome completo";/,
    "Edit mode must provide editable display_name input",
  );
  assert.match(
    workerSource,
    /emailEl\.className="user-email-readonly";\s*emailEl\.textContent=user\.email/,
    "Email must be rendered as read-only element in edit mode",
  );
  assert.match(
    workerSource,
    /cancelButton\.className="nav-button cancel-btn";\s*cancelButton\.type="button";\s*cancelButton\.textContent="Cancelar";\s*cancelButton\.disabled=false;\s*cancelButton\.onclick=\(\)=>renderViewRow\(\);/,
    "Edit mode must provide 'Cancelar' button that restores view mode without saving",
  );
  assert.match(
    workerSource,
    /saveButton\.className="nav-button primary save-btn";\s*saveButton\.type="button";\s*saveButton\.textContent="Salvar alterações";/,
    "Edit mode must provide 'Salvar alterações' button",
  );
});

test("Users table: saving updates displays 'Salvando...', blocks duplicate clicks, and validates self-lockout", () => {
  assert.match(
    workerSource,
    /if\(isSelf&&status\.value==="disabled"\)\{\s*showError\(\$\("admin-error"\),"Você não pode desativar o próprio acesso\."\);\s*return;?\s*\}/,
    "Frontend must prevent authenticated user from disabling their own account",
  );
  assert.match(
    workerSource,
    /if\(isSelf&&!\["ceo","cso","cmo"\]\.includes\(role\.value\)\)\{\s*showError\(\$\("admin-error"\),"Você não pode remover o próprio acesso executivo\."\);\s*return;?\s*\}/,
    "Frontend must prevent authenticated executive from demoting their own role",
  );
  assert.match(
    workerSource,
    /saveButton\.disabled=true;\s*cancelButton\.disabled=true;\s*saveButton\.textContent="Salvando\.\.\.";/,
    "Save button must show 'Salvando...' and disable action buttons during request",
  );
  assert.match(
    workerSource,
    /showError\(\$\("admin-success"\),"Usuário atualizado com sucesso\."\);/,
    "Success confirmation must be shown upon updating user",
  );
});

test("Backend: PATCH /users/:id accepts display_name, audits update, and validates fail-closed security", () => {
  assert.match(
    adminSource,
    /const displayName = cleanText\(body\.display_name, 120\);/,
    "Backend must accept and sanitize display_name",
  );
  assert.match(
    adminSource,
    /if \(userId === auth\.user\.id && status === "disabled"\) \{\s*return json\({\s*error: "Você não pode desativar o próprio acesso\."\s*\}, 400\);\s*\}/,
    "Backend must reject self-disable attempts",
  );
  assert.match(
    adminSource,
    /if \(userId === auth\.user\.id && !EXECUTIVE_ROLES\.has\(role\)\) \{\s*return json\({\s*error: "Você não pode remover o próprio acesso executivo\."\s*\}, 400\);\s*\}/,
    "Backend must reject self-demotion from executive roles",
  );
  assert.match(
    adminSource,
    /action: "user_updated",\s*details: \{ display_name: displayName, access_role: role, status, team_keys: teamKeys \},/,
    "Backend must log display_name, role, status and team_keys in audit table",
  );
});
