import { normalizeManagementKey } from "./team_reference.ts";

type Row = Record<string, unknown>;
type CountRow = { deals_count: number } & Record<string, string | number>;
type StageRow = { stage: string; deals_count: number; status_breakdown: CountRow[] };

function record(value: unknown): Row | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Row
    : null;
}

function count(value: unknown): number {
  const number = typeof value === "number" || typeof value === "string"
    ? Number(value)
    : NaN;
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

function countRows(value: unknown, label: "stage" | "status"): CountRow[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((candidate) => {
    const row = record(candidate);
    return row && typeof row[label] === "string" && row[label].trim()
      ? [{ [label]: row[label], deals_count: count(row.deals_count) }]
      : [];
  });
}

function mergeRows(rows: CountRow[], label: "stage" | "status"): CountRow[] {
  const grouped = new Map<string, CountRow>();
  for (const row of rows) {
    const key = normalizeManagementKey(String(row[label]));
    const current = grouped.get(key) ?? { [label]: row[label], deals_count: 0 };
    current.deals_count += row.deals_count;
    grouped.set(key, current);
  }
  return [...grouped.values()].sort((a, b) =>
    b.deals_count - a.deals_count ||
    String(a[label]).localeCompare(String(b[label]), "pt-BR")
  );
}

function total(rows: Array<{ deals_count: number }>): number {
  return rows.reduce((sum, row) => sum + row.deals_count, 0);
}

/** Rebuild every metric from already attributed, authorized teams only. */
export function authorizedFunnelSummary(summary: Row, allowedTeamKeys: string[]) {
  const funnel = record(summary.team_funnel);
  if (!funnel || !Array.isArray(funnel.team_breakdown)) return null;
  const allowed = new Set(allowedTeamKeys.map(normalizeManagementKey));
  const candidates = funnel.team_breakdown.flatMap((candidate) => {
    const row = record(candidate);
    return row && typeof row.team === "string" &&
        allowed.has(normalizeManagementKey(row.team))
      ? [row]
      : [];
  });
  // Marginal stage/status totals cannot recover the status of each stage.
  // An incompatible contract must never fall back to company-wide metrics.
  if (candidates.some((row) =>
    !Array.isArray(row.stage_breakdown) || !Array.isArray(row.status_breakdown) ||
    !Array.isArray(row.stage_status_breakdown)
  )) return null;

  const teamRows = candidates.map((row) => ({
    team: String(row.team),
    deals_count: count(row.deals_count),
    stage_breakdown: countRows(row.stage_breakdown, "stage"),
    status_breakdown: countRows(row.status_breakdown, "status"),
    stage_status_breakdown: (row.stage_status_breakdown as unknown[]).flatMap((candidate) => {
      const stage = record(candidate);
      return stage && typeof stage.stage === "string" && stage.stage.trim()
        ? [{
          stage: stage.stage,
          deals_count: count(stage.deals_count),
          status_breakdown: countRows(stage.status_breakdown, "status"),
        }]
        : [];
    }),
  }));

  const stages = new Map<string, StageRow>();
  for (const row of teamRows.flatMap((team) => team.stage_status_breakdown)) {
    const key = normalizeManagementKey(row.stage);
    const current = stages.get(key) ?? {
      stage: row.stage,
      deals_count: 0,
      status_breakdown: [],
    };
    current.deals_count += row.deals_count;
    current.status_breakdown = mergeRows(
      [...current.status_breakdown, ...row.status_breakdown], "status",
    );
    stages.set(key, current);
  }
  const stageStatusRows = [...stages.values()].sort((a, b) =>
    b.deals_count - a.deals_count || a.stage.localeCompare(b.stage, "pt-BR")
  );
  const isProposal = (row: StageRow) => normalizeManagementKey(row.stage) === "proposta";
  const openCount = (rows: CountRow[]) => total(rows.filter((row) =>
    ["aberto", "em aberto", "open"].includes(normalizeManagementKey(String(row.status)))
  ));
  const proposalStages = stageStatusRows.filter(isProposal);
  const proposalStatuses = mergeRows(proposalStages.flatMap((row) => row.status_breakdown), "status");
  const proposalCurrent = total(proposalStages);
  const proposalOpen = openCount(proposalStatuses);
  const scopedDeals = total(teamRows);
  const scope = "authorized_teams_only";

  // Deliberately do not spread the upstream summary, proposal or data_quality:
  // those objects contain totals across teams outside the caller's scope.
  return {
    scope,
    created_deals: scopedDeals,
    current_stage_breakdown: mergeRows(teamRows.flatMap((row) => row.stage_breakdown), "stage"),
    status_breakdown: mergeRows(teamRows.flatMap((row) => row.status_breakdown), "status"),
    stage_status_breakdown: stageStatusRows,
    team_funnel: {
      team_breakdown: teamRows,
      selected_team: teamRows.length === 1 ? teamRows[0] : null,
      team_filter: null,
      team_coverage: {
        created_deals_total: scopedDeals,
        assigned_deals: scopedDeals,
        unassigned_deals: 0,
        assignment_rate: scopedDeals > 0 ? 1 : 0,
        scope,
      },
    },
    proposal: {
      created_deals_currently_in_proposal: proposalCurrent,
      created_deals_in_proposal_stage_with_open_status: proposalOpen,
      current_proposal_stage_status_breakdown: proposalStatuses,
      team_breakdown: teamRows.flatMap((row) => {
        const proposals = row.stage_status_breakdown.filter(isProposal);
        const current = total(proposals);
        return current > 0 ? [{
          team: row.team,
          current_stage_deals_count: current,
          open_deals_count: openCount(proposals.flatMap((stage) => stage.status_breakdown)),
        }] : [];
      }),
      team_coverage: {
        proposal_open_total: proposalOpen,
        proposal_current_stage_total: proposalCurrent,
        assigned_open: proposalOpen,
        assigned_current_stage: proposalCurrent,
        scope,
      },
      proposals_generated_in_period: null,
      proposals_generated_status: "requires_stage_event_history",
    },
  };
}
