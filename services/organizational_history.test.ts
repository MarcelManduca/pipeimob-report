import assert from "node:assert/strict";
import test from "node:test";

import {
  currentSalesVersion,
  entityTypeForSourceObject,
  markMissingSalesFacts,
  reconcileDirectorySnapshot,
  reconcileMembershipPeriods,
  reconcileSalesFact,
  resolveLiveAssignment,
  resolveMembershipAt,
  resolveTeamOperationalStatus,
  resolveTeamStoreThroughManager,
  summarizeVistaOrganizationalCoverage,
  validateLiveDirectorySourceContract,
  type DirectoryEntity,
  type MembershipPeriod,
  type SalesFactState,
} from "./organizational_history.ts";

const firstSeenAt = "2026-08-01T12:00:00Z";

function person(overrides: Partial<DirectoryEntity> = {}): DirectoryEntity {
  return {
    sourceSystem: "vista",
    entityType: "person",
    sourceId: "person-1",
    currentName: "Pessoa Comercial",
    status: "active",
    firstSeenAt,
    lastSeenAt: firstSeenAt,
    missingSince: null,
    ...overrides,
  };
}

test("source object kinds do not turn access groups into teams", () => {
  assert.equal(entityTypeForSourceObject("store_or_branch"), "store");
  assert.equal(entityTypeForSourceObject("commercial_team"), "team");
  assert.equal(entityTypeForSourceObject("person"), "person");
  assert.equal(entityTypeForSourceObject("access_group"), null);
});

test("a live source contract requires explicit teams and store links", () => {
  const validation = validateLiveDirectorySourceContract({
    sourceSystem: "pipeimob",
    stableEntityIds: true,
    personDirectory: true,
    storeDirectory: true,
    commercialTeamDirectory: false,
    commercialTeamMemberships: false,
    teamStoreLinks: false,
    teamManagerLinks: false,
    personStoreLinks: false,
    activeState: true,
    completeSnapshotSignal: true,
    usesAccessGroupsAsCommercialTeams: true,
  });

  assert.equal(validation.ready, false);
  assert.deepEqual(validation.blockers, [
    "missing_commercial_team_directory",
    "missing_commercial_team_memberships",
    "missing_team_store_links",
    "access_groups_are_not_commercial_teams",
  ]);
});

test("a complete commercial directory contract is ready for an adapter", () => {
  const validation = validateLiveDirectorySourceContract({
    sourceSystem: "pipeimob",
    stableEntityIds: true,
    personDirectory: true,
    storeDirectory: true,
    commercialTeamDirectory: true,
    commercialTeamMemberships: true,
    teamStoreLinks: true,
    teamManagerLinks: false,
    personStoreLinks: false,
    activeState: true,
    completeSnapshotSignal: true,
    usesAccessGroupsAsCommercialTeams: false,
  });

  assert.deepEqual(validation, { ready: true, blockers: [] });
});

test("Vista can resolve a team store through the manager agency", () => {
  const resolution = resolveTeamStoreThroughManager(
    "team-campeche",
    [{
      sourceSystem: "vista",
      teamSourceId: "team-campeche",
      managerSourceId: "manager-fernanda",
      observedAt: "2026-09-06T12:00:00Z",
    }],
    [{
      sourceSystem: "vista",
      personSourceId: "manager-fernanda",
      storeSourceId: "agency-campeche",
      active: true,
      observedAt: "2026-09-06T12:00:00Z",
    }],
  );

  assert.deepEqual(resolution, {
    status: "resolved",
    teamSourceId: "team-campeche",
    managerSourceId: "manager-fernanda",
    storeSourceId: "agency-campeche",
    method: "manager_store",
  });
});

test("Vista team-store resolution never guesses across manager agencies", () => {
  const resolution = resolveTeamStoreThroughManager(
    "team-campeche",
    [{
      sourceSystem: "vista",
      teamSourceId: "team-campeche",
      managerSourceId: "manager-fernanda",
      observedAt: "2026-09-06T12:00:00Z",
    }],
    [
      {
        sourceSystem: "vista",
        personSourceId: "manager-fernanda",
        storeSourceId: "agency-campeche",
        active: true,
        observedAt: "2026-09-06T12:00:00Z",
      },
      {
        sourceSystem: "vista",
        personSourceId: "manager-fernanda",
        storeSourceId: "agency-centro",
        active: true,
        observedAt: "2026-09-06T12:00:00Z",
      },
    ],
  );

  assert.equal(resolution.status, "conflict");
  assert.equal(resolution.storeSourceId, null);
});

test("a Vista-derived store link satisfies the live contract", () => {
  const validation = validateLiveDirectorySourceContract({
    sourceSystem: "vista",
    stableEntityIds: true,
    personDirectory: true,
    storeDirectory: true,
    commercialTeamDirectory: true,
    commercialTeamMemberships: true,
    teamStoreLinks: false,
    teamManagerLinks: true,
    personStoreLinks: true,
    activeState: true,
    completeSnapshotSignal: true,
    usesAccessGroupsAsCommercialTeams: false,
  });

  assert.deepEqual(validation, { ready: true, blockers: [] });
});

test("declared active alone does not prove a team is operational", () => {
  assert.equal(
    resolveTeamOperationalStatus({
      declaredActive: true,
      activeMemberCount: 0,
      openDealCount: 0,
      recentSaleCount: 0,
    }),
    "unverified",
  );
  assert.equal(
    resolveTeamOperationalStatus({
      declaredActive: true,
      activeMemberCount: 1,
      openDealCount: 0,
      recentSaleCount: 0,
    }),
    "operational_active",
  );
});

test("Vista labels never qualify as stable organizational identities", () => {
  const coverage = summarizeVistaOrganizationalCoverage([{
    brokerSourceId: "broker-653",
    teamSourceId: null,
    teamName: "Equipe 1",
    storeSourceId: null,
    storeName: "Agência Campeche",
    managerSourceId: null,
    managerName: "Pessoa gerente",
  }]);

  assert.deepEqual(coverage.labelsWithoutStableIds, { team: 1, store: 1, manager: 1 });
  assert.equal(coverage.capabilities.historicalBrokerTeam, "blocked");
  assert.equal(coverage.safeForAutomaticIdentityResolution, false);
});

test("Vista stable IDs make direct organizational attribution ready", () => {
  const coverage = summarizeVistaOrganizationalCoverage([
    {
      brokerSourceId: "broker-1", teamSourceId: "team-1", teamName: "Equipe Um",
      storeSourceId: "store-1", storeName: "Loja Um",
      managerSourceId: "manager-1", managerName: "Gerente Um",
    },
    {
      brokerSourceId: "broker-2", teamSourceId: "team-2", teamName: "Equipe Dois",
      storeSourceId: "store-2", storeName: "Loja Dois",
      managerSourceId: "manager-2", managerName: "Gerente Dois",
    },
  ]);

  assert.deepEqual(coverage.stableRelationships, {
    brokerTeam: 2, teamStore: 2, teamManager: 2, managerStore: 2,
  });
  assert.equal(coverage.capabilities.historicalBrokerTeam, "ready");
  assert.equal(coverage.safeForAutomaticIdentityResolution, true);
});

test("Vista coverage reports partial stable-ID evidence without guessing", () => {
  const coverage = summarizeVistaOrganizationalCoverage([
    {
      brokerSourceId: "broker-1", teamSourceId: "team-1", teamName: "Equipe Um",
      storeSourceId: null, storeName: "Loja Um",
      managerSourceId: "manager-1", managerName: "Gerente Um",
    },
    {
      brokerSourceId: "broker-2", teamSourceId: null, teamName: "Equipe Dois",
      storeSourceId: null, storeName: "Loja Dois",
      managerSourceId: null, managerName: "Gerente Dois",
    },
  ]);

  assert.equal(coverage.capabilities.historicalBrokerTeam, "partial");
  assert.equal(coverage.capabilities.teamManager, "partial");
  assert.equal(coverage.capabilities.directTeamStore, "blocked");
  assert.equal(coverage.safeForAutomaticIdentityResolution, false);
});

test("a rename updates the live name without changing source identity", () => {
  const result = reconcileDirectorySnapshot(
    [person()],
    [{
      sourceSystem: "vista",
      entityType: "person",
      sourceId: "person-1",
      name: "Pessoa Comercial Renomeada",
      active: true,
    }],
    {
      sourceSystem: "vista",
      entityType: "person",
      observedAt: "2026-09-01T12:00:00Z",
      completeSnapshot: true,
    },
  );

  assert.equal(result.entities.length, 1);
  assert.equal(result.entities[0].sourceId, "person-1");
  assert.equal(result.entities[0].currentName, "Pessoa Comercial Renomeada");
  assert.equal(result.events[0].type, "renamed");
});

test("only a complete snapshot marks a missing source entity", () => {
  const partial = reconcileDirectorySnapshot(
    [person()],
    [],
    {
      sourceSystem: "vista",
      entityType: "person",
      observedAt: "2026-09-01T12:00:00Z",
      completeSnapshot: false,
    },
  );
  assert.equal(partial.entities[0].status, "active");

  const complete = reconcileDirectorySnapshot(
    partial.entities,
    [],
    {
      sourceSystem: "vista",
      entityType: "person",
      observedAt: "2026-09-02T12:00:00Z",
      completeSnapshot: true,
    },
  );
  assert.equal(complete.entities[0].status, "source_missing");
  assert.equal(complete.entities[0].lastSeenAt, firstSeenAt);
  assert.equal(complete.events[0].type, "source_missing");
});

test("a transfer closes the old period and opens the new one", () => {
  const periods: MembershipPeriod[] = [{
    sourceSystem: "pipeimob",
    personSourceId: "broker-1",
    teamSourceId: "team-alpha",
    storeSourceId: "store-centro",
    role: "broker",
    validFrom: "2026-01-01T00:00:00Z",
    validTo: null,
  }];
  const transferred = reconcileMembershipPeriods(periods, {
    sourceSystem: "pipeimob",
    personSourceId: "broker-1",
    teamSourceId: "team-beta",
    storeSourceId: "store-campeche",
    role: "broker",
    active: true,
    observedAt: "2026-09-01T00:00:00Z",
  });

  assert.equal(transferred[0].validTo, "2026-09-01T00:00:00Z");
  assert.equal(transferred[1].teamSourceId, "team-beta");
  assert.equal(
    resolveMembershipAt(
      transferred,
      "pipeimob",
      "broker-1",
      "broker",
      "2026-08-31T23:59:59Z",
    )?.teamSourceId,
    "team-alpha",
  );
  assert.equal(
    resolveMembershipAt(
      transferred,
      "pipeimob",
      "broker-1",
      "broker",
      "2026-09-01T00:00:00Z",
    )?.teamSourceId,
    "team-beta",
  );
});

test("deactivation closes membership without deleting its history", () => {
  const periods: MembershipPeriod[] = [{
    sourceSystem: "vista",
    personSourceId: "manager-1",
    teamSourceId: "team-alpha",
    storeSourceId: "store-centro",
    role: "manager",
    validFrom: "2026-01-01T00:00:00-03:00",
    validTo: null,
  }];
  const closed = reconcileMembershipPeriods(periods, {
    sourceSystem: "vista",
    personSourceId: "manager-1",
    teamSourceId: null,
    storeSourceId: null,
    role: "manager",
    active: false,
    observedAt: "2026-09-01T03:00:00Z",
  });

  assert.equal(closed.length, 1);
  assert.equal(closed[0].validTo, "2026-09-01T03:00:00Z");
  assert.equal(
    resolveMembershipAt(
      closed,
      "vista",
      "manager-1",
      "manager",
      "2026-08-31T23:59:59-03:00",
    )?.teamSourceId,
    "team-alpha",
  );
  assert.equal(
    resolveMembershipAt(
      closed,
      "vista",
      "manager-1",
      "manager",
      "2026-09-01T00:00:00-03:00",
    ),
    null,
  );
});

test("different live assignments are flagged instead of guessed", () => {
  const resolution = resolveLiveAssignment([
    {
      sourceSystem: "vista",
      teamCanonicalId: "team-alpha",
      storeCanonicalId: "store-centro",
      observedAt: "2026-09-01T12:00:00Z",
    },
    {
      sourceSystem: "pipeimob",
      teamCanonicalId: "team-beta",
      storeCanonicalId: "store-campeche",
      observedAt: "2026-09-01T12:01:00Z",
    },
  ]);

  assert.deepEqual(resolution, {
    status: "conflict",
    teamCanonicalId: null,
    storeCanonicalId: null,
    sources: ["pipeimob", "vista"],
  });
});

test("matching live evidence resolves one canonical assignment", () => {
  const resolution = resolveLiveAssignment([
    {
      sourceSystem: "vista",
      teamCanonicalId: "team-alpha",
      storeCanonicalId: null,
      observedAt: "2026-09-01T12:00:00Z",
    },
    {
      sourceSystem: "pipeimob",
      teamCanonicalId: "team-alpha",
      storeCanonicalId: "store-centro",
      observedAt: "2026-09-01T12:01:00Z",
    },
  ]);

  assert.equal(resolution.status, "resolved");
  assert.equal(resolution.teamCanonicalId, "team-alpha");
  assert.equal(resolution.storeCanonicalId, "store-centro");
});

function firstSale(): SalesFactState {
  return reconcileSalesFact(null, {
    sourceSystem: "pipeimob",
    transactionSourceId: "sale-1",
    observedAt: "2026-08-20T12:00:00Z",
    saleDate: "2026-08-20",
    vgv: 1_000_000,
    brokerSourceId: "broker-1",
    brokerNameAtSale: "Pessoa Comercial",
    teamSourceId: "team-alpha",
    teamNameAtSale: "Equipe Alpha",
    storeSourceId: "store-centro",
    storeNameAtSale: "Loja Centro",
    payloadHash: "hash-v1",
  });
}

test("a missing source transaction remains reportable", () => {
  const missing = markMissingSalesFacts(
    [firstSale()],
    "pipeimob",
    [],
    "2026-09-01T12:00:00Z",
    true,
  )[0];

  assert.equal(missing.sourceStatus, "source_missing");
  assert.equal(currentSalesVersion(missing).vgv, 1_000_000);
  assert.equal(currentSalesVersion(missing).teamNameAtSale, "Equipe Alpha");
});

test("an extinct team does not change the team photographed on a sale", () => {
  const sale = firstSale();
  const directory = reconcileDirectorySnapshot(
    [{
      sourceSystem: "pipeimob",
      entityType: "team",
      sourceId: "team-alpha",
      currentName: "Equipe Alpha",
      status: "active",
      firstSeenAt,
      lastSeenAt: firstSeenAt,
      missingSince: null,
    }],
    [{
      sourceSystem: "pipeimob",
      entityType: "team",
      sourceId: "team-alpha",
      name: "Equipe Alpha",
      active: false,
    }],
    {
      sourceSystem: "pipeimob",
      entityType: "team",
      observedAt: "2026-09-01T12:00:00Z",
      completeSnapshot: true,
    },
  );

  assert.equal(directory.entities[0].status, "inactive");
  assert.equal(currentSalesVersion(sale).teamNameAtSale, "Equipe Alpha");
  assert.equal(currentSalesVersion(sale).vgv, 1_000_000);
});

test("a source correction creates a new version instead of erasing history", () => {
  const original = firstSale();
  const corrected = reconcileSalesFact(original, {
    sourceSystem: "pipeimob",
    transactionSourceId: "sale-1",
    observedAt: "2026-09-02T12:00:00Z",
    saleDate: "2026-08-20",
    vgv: 1_100_000,
    brokerSourceId: "broker-1",
    brokerNameAtSale: "Pessoa Comercial",
    teamSourceId: "team-alpha",
    teamNameAtSale: "Equipe Alpha",
    storeSourceId: "store-centro",
    storeNameAtSale: "Loja Centro",
    payloadHash: "hash-v2",
  });

  assert.equal(corrected.versions.length, 2);
  assert.equal(corrected.versions[0].validTo, "2026-09-02T12:00:00Z");
  assert.equal(corrected.versions[1].changeReason, "source_correction");
  assert.equal(currentSalesVersion(corrected).vgv, 1_100_000);
});

test("re-fetching an unchanged transaction is idempotent", () => {
  const original = firstSale();
  const repeated = reconcileSalesFact(original, {
    sourceSystem: "pipeimob",
    transactionSourceId: "sale-1",
    observedAt: "2026-09-03T12:00:00Z",
    saleDate: "2026-08-20",
    vgv: 1_000_000,
    brokerSourceId: "broker-1",
    brokerNameAtSale: "Pessoa Comercial",
    teamSourceId: "team-alpha",
    teamNameAtSale: "Equipe Alpha",
    storeSourceId: "store-centro",
    storeNameAtSale: "Loja Centro",
    payloadHash: "hash-v1",
  });

  assert.equal(repeated.versions.length, 1);
  assert.equal(repeated.lastSeenAt, "2026-09-03T12:00:00Z");
});
