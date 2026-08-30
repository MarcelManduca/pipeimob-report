import assert from "node:assert/strict";
import test from "node:test";

import {
  buildManagerTeamIndex,
  resolveManagerTeam,
  type ManagerTeamReference,
} from "./team_reference.ts";

function reference(
  overrides: Partial<ManagerTeamReference> = {},
): ManagerTeamReference {
  return {
    manager_key: "mauricio villaca",
    manager_name: "Maurício Villaça",
    team_key: "equipe alfa",
    team_name: "Equipe Alfa",
    valid_from: "2025-01-01",
    valid_to: "2025-12-31",
    source_updated_through: "2026-07-15",
    review_required: true,
    ...overrides,
  };
}

test("resolves accents and date-effective manager mapping", () => {
  const index = buildManagerTeamIndex([
    reference(),
    reference({
      team_key: "equipe iron",
      team_name: "Equipe Iron",
      valid_from: "2026-01-01",
      valid_to: null,
    }),
  ]);

  assert.deepEqual(resolveManagerTeam(index, "MAURICIO VILLAÇA", "2025-08-01"), {
    status: "resolved",
    teamKey: "equipe alfa",
    teamName: "Equipe Alfa",
    reviewRequired: true,
  });
  assert.equal(
    resolveManagerTeam(index, "Mauricio Villaca", "2026-08-01").teamName,
    "Equipe Iron",
  );
});

test("does not guess when effective periods overlap across teams", () => {
  const index = buildManagerTeamIndex([
    reference({ valid_to: null }),
    reference({
      team_key: "equipe iron",
      team_name: "Equipe Iron",
      valid_from: "2025-06-01",
      valid_to: null,
    }),
  ]);

  assert.equal(
    resolveManagerTeam(index, "Mauricio Villaca", "2026-08-01").status,
    "ambiguous",
  );
});

test("returns unresolved outside the known interval", () => {
  const index = buildManagerTeamIndex([reference()]);
  assert.equal(
    resolveManagerTeam(index, "Mauricio Villaca", "2024-12-31").status,
    "unresolved",
  );
});
