export type ManagerTeamReference = {
  manager_key: string;
  manager_name: string;
  team_key: string;
  team_name: string;
  valid_from: string;
  valid_to: string | null;
  source_updated_through: string;
  review_required: boolean;
};

export type BrokerTeamReference = {
  broker_key: string;
  broker_name: string;
  team_key: string;
  team_name: string;
  sale_date: string;
  source_updated_through: string;
};

export type ManagerTeamResolution =
  | {
    status: "resolved";
    teamKey: string;
    teamName: string;
    reviewRequired: boolean;
  }
  | {
    status: "ambiguous" | "unresolved";
    teamKey: null;
    teamName: null;
    reviewRequired: false;
  };

export function normalizeManagementKey(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLocaleLowerCase("pt-BR");
}

export function buildManagerTeamIndex(
  references: ManagerTeamReference[],
): Map<string, ManagerTeamReference[]> {
  const index = new Map<string, ManagerTeamReference[]>();
  for (const reference of references) {
    const managerKey = normalizeManagementKey(
      reference.manager_key || reference.manager_name,
    );
    if (!managerKey || !reference.team_name || !reference.valid_from) continue;
    const history = index.get(managerKey) ?? [];
    history.push(reference);
    index.set(managerKey, history);
  }
  for (const history of index.values()) {
    history.sort((a, b) =>
      String(a.valid_from).localeCompare(String(b.valid_from)) ||
      String(a.team_key).localeCompare(String(b.team_key))
    );
  }
  return index;
}

export function buildBrokerTeamIndex(
  references: BrokerTeamReference[],
): Map<string, BrokerTeamReference[]> {
  const index = new Map<string, BrokerTeamReference[]>();
  for (const reference of references) {
    const brokerKey = normalizeManagementKey(
      reference.broker_key || reference.broker_name,
    );
    if (!brokerKey || !reference.team_name || !reference.sale_date) continue;
    const history = index.get(brokerKey) ?? [];
    history.push(reference);
    index.set(brokerKey, history);
  }
  for (const history of index.values()) {
    history.sort((a, b) =>
      String(a.sale_date).localeCompare(String(b.sale_date)) ||
      String(a.team_key).localeCompare(String(b.team_key))
    );
  }
  return index;
}

export function resolveBrokerTeam(
  index: Map<string, BrokerTeamReference[]>,
  broker: string,
  targetDate: string,
): ManagerTeamResolution {
  const brokerKey = normalizeManagementKey(broker);
  if (!brokerKey || !/^\d{4}-\d{2}-\d{2}$/.test(targetDate)) {
    return {
      status: "unresolved",
      teamKey: null,
      teamName: null,
      reviewRequired: false,
    };
  }
  const eligible = (index.get(brokerKey) ?? []).filter((reference) =>
    String(reference.sale_date ?? "").slice(0, 10) <= targetDate
  );
  const latestDate = eligible.reduce((latest, reference) => {
    const date = String(reference.sale_date ?? "").slice(0, 10);
    return date > latest ? date : latest;
  }, "");
  if (!latestDate) {
    return {
      status: "unresolved",
      teamKey: null,
      teamName: null,
      reviewRequired: false,
    };
  }
  const latest = eligible.filter((reference) =>
    String(reference.sale_date ?? "").slice(0, 10) === latestDate
  );
  const distinctTeams = new Map<string, BrokerTeamReference>();
  for (const reference of latest) {
    const teamKey = normalizeManagementKey(
      reference.team_key || reference.team_name,
    );
    if (teamKey) distinctTeams.set(teamKey, reference);
  }
  if (distinctTeams.size !== 1) {
    return {
      status: distinctTeams.size > 1 ? "ambiguous" : "unresolved",
      teamKey: null,
      teamName: null,
      reviewRequired: false,
    };
  }
  const [teamKey, reference] = [...distinctTeams.entries()][0];
  return {
    status: "resolved",
    teamKey,
    teamName: reference.team_name,
    reviewRequired: false,
  };
}

export function resolveManagerTeam(
  index: Map<string, ManagerTeamReference[]>,
  manager: string,
  saleDate: string,
): ManagerTeamResolution {
  const managerKey = normalizeManagementKey(manager);
  if (!managerKey || !/^\d{4}-\d{2}-\d{2}$/.test(saleDate)) {
    return {
      status: "unresolved",
      teamKey: null,
      teamName: null,
      reviewRequired: false,
    };
  }

  const matches = (index.get(managerKey) ?? []).filter((reference) => {
    const validFrom = String(reference.valid_from ?? "").slice(0, 10);
    const validTo = reference.valid_to
      ? String(reference.valid_to).slice(0, 10)
      : null;
    return Boolean(
      validFrom && validFrom <= saleDate && (!validTo || validTo >= saleDate),
    );
  });
  const distinctTeams = new Map<string, ManagerTeamReference>();
  for (const reference of matches) {
    const teamKey = normalizeManagementKey(
      reference.team_key || reference.team_name,
    );
    if (teamKey) distinctTeams.set(teamKey, reference);
  }
  if (distinctTeams.size > 1) {
    return {
      status: "ambiguous",
      teamKey: null,
      teamName: null,
      reviewRequired: false,
    };
  }
  if (distinctTeams.size === 1) {
    const [teamKey, reference] = [...distinctTeams.entries()][0];
    return {
      status: "resolved",
      teamKey,
      teamName: reference.team_name,
      reviewRequired: Boolean(reference.review_required),
    };
  }
  return {
    status: "unresolved",
    teamKey: null,
    teamName: null,
    reviewRequired: false,
  };
}
