export type SourceSystem = "vista" | "pipeimob";
export type EntityType = "store" | "team" | "person";
export type DirectoryStatus = "active" | "inactive" | "source_missing";
export type PersonRole = "broker" | "manager";

export type SourceObjectKind =
  | "store_or_branch"
  | "commercial_team"
  | "access_group"
  | "person";

export type LiveDirectorySourceContract = {
  sourceSystem: SourceSystem;
  stableEntityIds: boolean;
  personDirectory: boolean;
  storeDirectory: boolean;
  commercialTeamDirectory: boolean;
  commercialTeamMemberships: boolean;
  teamStoreLinks: boolean;
  teamManagerLinks: boolean;
  personStoreLinks: boolean;
  activeState: boolean;
  completeSnapshotSignal: boolean;
  usesAccessGroupsAsCommercialTeams: boolean;
};

export type LiveDirectoryContractBlocker =
  | "unstable_entity_ids"
  | "missing_person_directory"
  | "missing_store_directory"
  | "missing_commercial_team_directory"
  | "missing_commercial_team_memberships"
  | "missing_team_store_links"
  | "missing_active_state"
  | "missing_complete_snapshot_signal"
  | "access_groups_are_not_commercial_teams";

export type LiveDirectoryContractValidation = {
  ready: boolean;
  blockers: LiveDirectoryContractBlocker[];
};

export type TeamManagerObservation = {
  sourceSystem: "vista";
  teamSourceId: string;
  managerSourceId: string | null;
  observedAt: string;
};

export type PersonStoreObservation = {
  sourceSystem: "vista";
  personSourceId: string;
  storeSourceId: string | null;
  active: boolean;
  observedAt: string;
};

export type TeamStoreResolution = {
  status: "resolved" | "conflict" | "unresolved";
  teamSourceId: string;
  managerSourceId: string | null;
  storeSourceId: string | null;
  method: "manager_store" | null;
};

export type TeamOperationalEvidence = {
  declaredActive: boolean | null;
  activeMemberCount: number | null;
  openDealCount: number | null;
  recentSaleCount: number | null;
};

export type TeamOperationalStatus =
  | "operational_active"
  | "declared_inactive"
  | "unverified";

export type VistaOrganizationalEvidence = {
  brokerSourceId: string | null;
  teamSourceId: string | null;
  teamName: string | null;
  storeSourceId: string | null;
  storeName: string | null;
  managerSourceId: string | null;
  managerName: string | null;
};

export type EvidenceCoverageStatus = "ready" | "partial" | "blocked";

export type VistaOrganizationalCoverage = {
  observationCount: number;
  stableIds: { broker: number; team: number; store: number; manager: number };
  labelsWithoutStableIds: { team: number; store: number; manager: number };
  stableRelationships: {
    brokerTeam: number;
    teamStore: number;
    teamManager: number;
    managerStore: number;
  };
  capabilities: {
    historicalBrokerTeam: EvidenceCoverageStatus;
    directTeamStore: EvidenceCoverageStatus;
    teamManager: EvidenceCoverageStatus;
    derivedTeamStore: EvidenceCoverageStatus;
  };
  safeForAutomaticIdentityResolution: boolean;
};

export type DirectoryEntity = {
  sourceSystem: SourceSystem;
  entityType: EntityType;
  sourceId: string;
  currentName: string;
  status: DirectoryStatus;
  firstSeenAt: string;
  lastSeenAt: string;
  missingSince: string | null;
};

export type DirectoryObservation = {
  sourceSystem: SourceSystem;
  entityType: EntityType;
  sourceId: string;
  name: string;
  active: boolean;
};

export type DirectoryEvent = {
  type: "created" | "renamed" | "deactivated" | "reactivated" |
    "source_missing";
  key: string;
  observedAt: string;
  previousName: string | null;
  currentName: string;
};

export type DirectoryReconciliation = {
  entities: DirectoryEntity[];
  events: DirectoryEvent[];
};

export type MembershipPeriod = {
  sourceSystem: SourceSystem;
  personSourceId: string;
  teamSourceId: string;
  storeSourceId: string | null;
  role: PersonRole;
  validFrom: string;
  validTo: string | null;
};

export type MembershipObservation = {
  sourceSystem: SourceSystem;
  personSourceId: string;
  teamSourceId: string | null;
  storeSourceId: string | null;
  role: PersonRole;
  active: boolean;
  observedAt: string;
};

export type CanonicalAssignmentEvidence = {
  sourceSystem: SourceSystem;
  teamCanonicalId: string | null;
  storeCanonicalId: string | null;
  observedAt: string;
};

export type CanonicalAssignmentResolution = {
  status: "resolved" | "conflict" | "unresolved";
  teamCanonicalId: string | null;
  storeCanonicalId: string | null;
  sources: SourceSystem[];
};

export type SalesFactVersion = {
  version: number;
  validFrom: string;
  validTo: string | null;
  saleDate: string;
  vgv: number;
  brokerSourceId: string | null;
  brokerNameAtSale: string | null;
  teamSourceId: string | null;
  teamNameAtSale: string | null;
  storeSourceId: string | null;
  storeNameAtSale: string | null;
  payloadHash: string;
  changeReason: "first_observation" | "source_correction";
};

export type SalesFactState = {
  sourceSystem: SourceSystem;
  transactionSourceId: string;
  sourceStatus: "present" | "source_missing";
  firstSeenAt: string;
  lastSeenAt: string;
  missingSince: string | null;
  versions: SalesFactVersion[];
};

export type SalesFactObservation = Omit<
  SalesFactVersion,
  "version" | "validFrom" | "validTo" | "changeReason"
> & {
  sourceSystem: SourceSystem;
  transactionSourceId: string;
  observedAt: string;
};

function required(value: string, label: string): string {
  const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!normalized) throw new Error(`${label} is required`);
  return normalized;
}

function assertTimestamp(value: string, label: string): string {
  const normalized = required(value, label);
  if (Number.isNaN(Date.parse(normalized))) {
    throw new Error(`${label} must be an ISO timestamp`);
  }
  return normalized;
}

function timestampValue(value: string, label: string): number {
  return Date.parse(assertTimestamp(value, label));
}

function assertDateOnly(value: string, label: string): string {
  const normalized = required(value, label);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(normalized)) {
    throw new Error(`${label} must use YYYY-MM-DD`);
  }
  const parsed = new Date(`${normalized}T00:00:00Z`);
  if (Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== normalized) {
    throw new Error(`${label} must be a valid calendar date`);
  }
  return normalized;
}

export function entityTypeForSourceObject(
  kind: SourceObjectKind,
): EntityType | null {
  if (kind === "store_or_branch") return "store";
  if (kind === "commercial_team") return "team";
  if (kind === "person") return "person";
  return null;
}

export function validateLiveDirectorySourceContract(
  contract: LiveDirectorySourceContract,
): LiveDirectoryContractValidation {
  const blockers: LiveDirectoryContractBlocker[] = [];
  if (!contract.stableEntityIds) blockers.push("unstable_entity_ids");
  if (!contract.personDirectory) blockers.push("missing_person_directory");
  if (!contract.storeDirectory) blockers.push("missing_store_directory");
  if (!contract.commercialTeamDirectory) {
    blockers.push("missing_commercial_team_directory");
  }
  if (!contract.commercialTeamMemberships) {
    blockers.push("missing_commercial_team_memberships");
  }
  const derivedTeamStoreLinks =
    contract.teamManagerLinks && contract.personStoreLinks;
  if (!contract.teamStoreLinks && !derivedTeamStoreLinks) {
    blockers.push("missing_team_store_links");
  }
  if (!contract.activeState) blockers.push("missing_active_state");
  if (!contract.completeSnapshotSignal) {
    blockers.push("missing_complete_snapshot_signal");
  }
  if (contract.usesAccessGroupsAsCommercialTeams) {
    blockers.push("access_groups_are_not_commercial_teams");
  }
  return { ready: blockers.length === 0, blockers };
}

function present(value: string | null): boolean {
  return typeof value === "string" && value.trim().length > 0;
}

function coverageStatus(count: number, total: number): EvidenceCoverageStatus {
  if (total > 0 && count === total) return "ready";
  if (count > 0) return "partial";
  return "blocked";
}

/** Aggregate-only capability evidence. Names are never identity keys. */
export function summarizeVistaOrganizationalCoverage(
  observations: VistaOrganizationalEvidence[],
): VistaOrganizationalCoverage {
  const total = observations.length;
  const count = (predicate: (item: VistaOrganizationalEvidence) => boolean) =>
    observations.filter(predicate).length;
  const broker = count((item) => present(item.brokerSourceId));
  const team = count((item) => present(item.teamSourceId));
  const store = count((item) => present(item.storeSourceId));
  const manager = count((item) => present(item.managerSourceId));
  const brokerTeam = count((item) =>
    present(item.brokerSourceId) && present(item.teamSourceId));
  const teamStore = count((item) =>
    present(item.teamSourceId) && present(item.storeSourceId));
  const teamManager = count((item) =>
    present(item.teamSourceId) && present(item.managerSourceId));
  const managerStore = count((item) =>
    present(item.managerSourceId) && present(item.storeSourceId));
  const capabilities = {
    historicalBrokerTeam: coverageStatus(brokerTeam, total),
    directTeamStore: coverageStatus(teamStore, total),
    teamManager: coverageStatus(teamManager, total),
    derivedTeamStore: coverageStatus(managerStore, total),
  };

  return {
    observationCount: total,
    stableIds: { broker, team, store, manager },
    labelsWithoutStableIds: {
      team: count((item) => present(item.teamName) && !present(item.teamSourceId)),
      store: count((item) => present(item.storeName) && !present(item.storeSourceId)),
      manager: count((item) =>
        present(item.managerName) && !present(item.managerSourceId)),
    },
    stableRelationships: { brokerTeam, teamStore, teamManager, managerStore },
    capabilities,
    safeForAutomaticIdentityResolution:
      capabilities.historicalBrokerTeam === "ready" &&
      (capabilities.directTeamStore === "ready" ||
        (capabilities.teamManager === "ready" &&
          capabilities.derivedTeamStore === "ready")),
  };
}

export function resolveTeamStoreThroughManager(
  teamSourceId: string,
  teamManagers: TeamManagerObservation[],
  personStores: PersonStoreObservation[],
): TeamStoreResolution {
  const normalizedTeamId = required(teamSourceId, "teamSourceId");
  const managerIds = new Set<string>();
  for (const observation of teamManagers) {
    assertTimestamp(observation.observedAt, "observedAt");
    if (required(observation.teamSourceId, "teamSourceId") !== normalizedTeamId) {
      continue;
    }
    if (observation.managerSourceId) {
      managerIds.add(required(observation.managerSourceId, "managerSourceId"));
    }
  }
  if (managerIds.size !== 1) {
    return {
      status: managerIds.size > 1 ? "conflict" : "unresolved",
      teamSourceId: normalizedTeamId,
      managerSourceId: null,
      storeSourceId: null,
      method: null,
    };
  }

  const managerSourceId = [...managerIds][0];
  const storeIds = new Set<string>();
  for (const observation of personStores) {
    assertTimestamp(observation.observedAt, "observedAt");
    if (
      observation.active &&
      required(observation.personSourceId, "personSourceId") === managerSourceId &&
      observation.storeSourceId
    ) {
      storeIds.add(required(observation.storeSourceId, "storeSourceId"));
    }
  }
  if (storeIds.size !== 1) {
    return {
      status: storeIds.size > 1 ? "conflict" : "unresolved",
      teamSourceId: normalizedTeamId,
      managerSourceId,
      storeSourceId: null,
      method: null,
    };
  }
  return {
    status: "resolved",
    teamSourceId: normalizedTeamId,
    managerSourceId,
    storeSourceId: [...storeIds][0],
    method: "manager_store",
  };
}

function assertOptionalCount(value: number | null, label: string): number | null {
  if (value === null) return null;
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer or null`);
  }
  return value;
}

export function resolveTeamOperationalStatus(
  evidence: TeamOperationalEvidence,
): TeamOperationalStatus {
  const activeMemberCount = assertOptionalCount(
    evidence.activeMemberCount,
    "activeMemberCount",
  );
  const openDealCount = assertOptionalCount(evidence.openDealCount, "openDealCount");
  const recentSaleCount = assertOptionalCount(
    evidence.recentSaleCount,
    "recentSaleCount",
  );
  if (
    (activeMemberCount ?? 0) > 0 ||
    (openDealCount ?? 0) > 0 ||
    (recentSaleCount ?? 0) > 0
  ) return "operational_active";
  if (evidence.declaredActive === false) return "declared_inactive";
  return "unverified";
}

export function directoryEntityKey(
  sourceSystem: SourceSystem,
  entityType: EntityType,
  sourceId: string,
): string {
  return `${sourceSystem}:${entityType}:${required(sourceId, "sourceId")}`;
}

export function reconcileDirectorySnapshot(
  existing: DirectoryEntity[],
  observations: DirectoryObservation[],
  options: {
    sourceSystem: SourceSystem;
    entityType: EntityType;
    observedAt: string;
    completeSnapshot: boolean;
  },
): DirectoryReconciliation {
  const observedAt = assertTimestamp(options.observedAt, "observedAt");
  const entities = existing.map((entity) => ({ ...entity }));
  const index = new Map<string, DirectoryEntity>();
  const events: DirectoryEvent[] = [];

  for (const entity of entities) {
    index.set(
      directoryEntityKey(
        entity.sourceSystem,
        entity.entityType,
        entity.sourceId,
      ),
      entity,
    );
  }

  const observedKeys = new Set<string>();
  for (const observation of observations) {
    if (
      observation.sourceSystem !== options.sourceSystem ||
      observation.entityType !== options.entityType
    ) {
      throw new Error("observation is outside the reconciled source scope");
    }
    const key = directoryEntityKey(
      observation.sourceSystem,
      observation.entityType,
      observation.sourceId,
    );
    const currentName = required(observation.name, "name");
    if (observedKeys.has(key)) {
      throw new Error(`duplicate observation for ${key}`);
    }
    observedKeys.add(key);

    const current = index.get(key);
    if (!current) {
      const created: DirectoryEntity = {
        sourceSystem: observation.sourceSystem,
        entityType: observation.entityType,
        sourceId: required(observation.sourceId, "sourceId"),
        currentName,
        status: observation.active ? "active" : "inactive",
        firstSeenAt: observedAt,
        lastSeenAt: observedAt,
        missingSince: null,
      };
      entities.push(created);
      index.set(key, created);
      events.push({
        type: "created",
        key,
        observedAt,
        previousName: null,
        currentName,
      });
      continue;
    }

    const previousName = current.currentName;
    const previousStatus = current.status;
    current.currentName = currentName;
    current.status = observation.active ? "active" : "inactive";
    current.lastSeenAt = observedAt;
    current.missingSince = null;

    if (previousName !== currentName) {
      events.push({
        type: "renamed",
        key,
        observedAt,
        previousName,
        currentName,
      });
    }
    if (previousStatus === "active" && !observation.active) {
      events.push({
        type: "deactivated",
        key,
        observedAt,
        previousName,
        currentName,
      });
    } else if (
      observation.active &&
      (previousStatus === "inactive" || previousStatus === "source_missing")
    ) {
      events.push({
        type: "reactivated",
        key,
        observedAt,
        previousName,
        currentName,
      });
    }
  }

  if (options.completeSnapshot) {
    for (const entity of entities) {
      if (
        entity.sourceSystem !== options.sourceSystem ||
        entity.entityType !== options.entityType
      ) continue;
      const key = directoryEntityKey(
        entity.sourceSystem,
        entity.entityType,
        entity.sourceId,
      );
      if (observedKeys.has(key) || entity.status !== "active") continue;
      entity.status = "source_missing";
      entity.missingSince = observedAt;
      events.push({
        type: "source_missing",
        key,
        observedAt,
        previousName: entity.currentName,
        currentName: entity.currentName,
      });
    }
  }

  return { entities, events };
}

export function reconcileMembershipPeriods(
  existing: MembershipPeriod[],
  observation: MembershipObservation,
): MembershipPeriod[] {
  const observedAt = assertTimestamp(observation.observedAt, "observedAt");
  const personSourceId = required(
    observation.personSourceId,
    "personSourceId",
  );
  const periods = existing.map((period) => ({ ...period }));
  const open = periods.filter((period) =>
    period.sourceSystem === observation.sourceSystem &&
    period.personSourceId === personSourceId &&
    period.role === observation.role &&
    period.validTo === null
  );
  if (open.length > 1) throw new Error("multiple open membership periods");
  const current = open[0] ?? null;

  if (!observation.active || !observation.teamSourceId) {
    if (current) {
      if (
        timestampValue(observedAt, "observedAt") <=
          timestampValue(current.validFrom, "validFrom")
      ) {
        throw new Error("membership closure must follow validFrom");
      }
      current.validTo = observedAt;
    }
    return periods;
  }

  const teamSourceId = required(observation.teamSourceId, "teamSourceId");
  const storeSourceId = observation.storeSourceId
    ? required(observation.storeSourceId, "storeSourceId")
    : null;
  if (
    current?.teamSourceId === teamSourceId &&
    current.storeSourceId === storeSourceId
  ) {
    return periods;
  }
  if (current) {
    if (
      timestampValue(observedAt, "observedAt") <=
        timestampValue(current.validFrom, "validFrom")
    ) {
      throw new Error("membership transfer must follow validFrom");
    }
    current.validTo = observedAt;
  }
  periods.push({
    sourceSystem: observation.sourceSystem,
    personSourceId,
    teamSourceId,
    storeSourceId,
    role: observation.role,
    validFrom: observedAt,
    validTo: null,
  });
  return periods;
}

export function resolveMembershipAt(
  periods: MembershipPeriod[],
  sourceSystem: SourceSystem,
  personSourceId: string,
  role: PersonRole,
  occurredAt: string,
): MembershipPeriod | null {
  const target = timestampValue(occurredAt, "occurredAt");
  const matches = periods.filter((period) =>
    period.sourceSystem === sourceSystem &&
    period.personSourceId === personSourceId &&
    period.role === role &&
    timestampValue(period.validFrom, "validFrom") <= target &&
    (period.validTo === null || target < timestampValue(period.validTo, "validTo"))
  );
  if (matches.length > 1) throw new Error("ambiguous membership history");
  return matches[0] ?? null;
}

export function resolveLiveAssignment(
  evidence: CanonicalAssignmentEvidence[],
): CanonicalAssignmentResolution {
  const sources = [...new Set(evidence.map((item) => item.sourceSystem))]
    .sort();
  const teamIds = new Set<string>();
  const storeIds = new Set<string>();
  for (const item of evidence) {
    assertTimestamp(item.observedAt, "observedAt");
    if (item.teamCanonicalId) {
      teamIds.add(required(item.teamCanonicalId, "teamCanonicalId"));
    }
    if (item.storeCanonicalId) {
      storeIds.add(required(item.storeCanonicalId, "storeCanonicalId"));
    }
  }
  if (teamIds.size > 1 || storeIds.size > 1) {
    return {
      status: "conflict",
      teamCanonicalId: null,
      storeCanonicalId: null,
      sources,
    };
  }
  if (teamIds.size === 0) {
    return {
      status: "unresolved",
      teamCanonicalId: null,
      storeCanonicalId: null,
      sources,
    };
  }
  return {
    status: "resolved",
    teamCanonicalId: [...teamIds][0],
    storeCanonicalId: storeIds.size ? [...storeIds][0] : null,
    sources,
  };
}

export function reconcileSalesFact(
  current: SalesFactState | null,
  observation: SalesFactObservation,
): SalesFactState {
  const transactionSourceId = required(
    observation.transactionSourceId,
    "transactionSourceId",
  );
  const observedAt = assertTimestamp(observation.observedAt, "observedAt");
  const payloadHash = required(observation.payloadHash, "payloadHash");
  const saleDate = assertDateOnly(observation.saleDate, "saleDate");
  if (!Number.isFinite(observation.vgv) || observation.vgv < 0) {
    throw new Error("vgv must be a non-negative finite number");
  }
  if (
    current &&
    (current.sourceSystem !== observation.sourceSystem ||
      current.transactionSourceId !== transactionSourceId)
  ) {
    throw new Error("observation does not match the sales fact identity");
  }

  const state: SalesFactState = current
    ? {
      ...current,
      versions: current.versions.map((version) => ({ ...version })),
    }
    : {
      sourceSystem: observation.sourceSystem,
      transactionSourceId,
      sourceStatus: "present",
      firstSeenAt: observedAt,
      lastSeenAt: observedAt,
      missingSince: null,
      versions: [],
    };
  state.sourceStatus = "present";
  state.lastSeenAt = observedAt;
  state.missingSince = null;

  const open = state.versions.filter((version) => version.validTo === null);
  if (open.length > 1) throw new Error("multiple open sales fact versions");
  const currentVersion = open[0] ?? null;
  if (currentVersion?.payloadHash === payloadHash) return state;
  if (currentVersion) {
    if (
      timestampValue(observedAt, "observedAt") <=
        timestampValue(currentVersion.validFrom, "validFrom")
    ) {
      throw new Error("sales correction must follow the current version");
    }
    currentVersion.validTo = observedAt;
  }
  state.versions.push({
    version: state.versions.reduce(
      (highest, version) => Math.max(highest, version.version),
      0,
    ) + 1,
    validFrom: observedAt,
    validTo: null,
    saleDate,
    vgv: observation.vgv,
    brokerSourceId: observation.brokerSourceId,
    brokerNameAtSale: observation.brokerNameAtSale,
    teamSourceId: observation.teamSourceId,
    teamNameAtSale: observation.teamNameAtSale,
    storeSourceId: observation.storeSourceId,
    storeNameAtSale: observation.storeNameAtSale,
    payloadHash,
    changeReason: currentVersion ? "source_correction" : "first_observation",
  });
  return state;
}

export function markMissingSalesFacts(
  facts: SalesFactState[],
  sourceSystem: SourceSystem,
  seenTransactionIds: Iterable<string>,
  observedAt: string,
  completeSnapshot: boolean,
): SalesFactState[] {
  if (!completeSnapshot) return facts.map((fact) => ({ ...fact }));
  const timestamp = assertTimestamp(observedAt, "observedAt");
  const seen = new Set([...seenTransactionIds].map((id) => required(id, "id")));
  return facts.map((fact) => {
    if (
      fact.sourceSystem !== sourceSystem ||
      seen.has(fact.transactionSourceId) ||
      fact.sourceStatus === "source_missing"
    ) return { ...fact };
    return {
      ...fact,
      sourceStatus: "source_missing",
      missingSince: timestamp,
      versions: fact.versions.map((version) => ({ ...version })),
    };
  });
}

export function currentSalesVersion(
  fact: SalesFactState,
): SalesFactVersion {
  const current = fact.versions.filter((version) => version.validTo === null);
  if (current.length !== 1) {
    throw new Error("sales fact has no unique current version");
  }
  return current[0];
}
