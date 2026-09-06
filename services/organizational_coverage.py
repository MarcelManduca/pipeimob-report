"""Aggregate organizational coverage diagnostic service (v4).

Evaluates coverage of stable IDs across the 4 core organizational dimensions:
1. broker -> team (corretor -> equipe)
2. team -> manager (equipe -> gerente)
3. manager -> agency/store (gerente -> agencia/loja)
4. team -> agency/store (equipe -> agencia/loja)

Adheres strictly to all requirements:
- Users with role_type 'unknown' are NEVER treated as brokers and do NOT inflate broker totals.
- Temporal semantics applied to all dimensions: distinct associations on different dates
  represent historical_change_evidence, not conflict. Simultaneous conflicting assignments
  on the same snapshot or same date produce conflict.
- Per-source metadata (users, deals) tracked with requested, successful, complete, truncated, error_code.
- Overall complete is True ONLY if all requested sources succeeded without truncation and fields are supported.
- Supported is False when no valid fields are negotiated.
- Returns only aggregate metrics, zero personal data.
"""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Sequence, Set


StatusType = Literal["ready", "partial", "blocked"]


GENERIC_NON_ORG_FIELDS = {
    "codigo",
    "status",
    "ativo",
    "cargo",
    "funcao",
    "perfil",
    "codigopipe",
    "corretornegocio",
    "etapaatual",
    "datainicial",
    "datafinal",
}


def compute_dimension_status(
    total: int,
    resolved: int,
    conflicts: int,
    field_probed_and_available: bool,
    truncated_or_incomplete: bool = False,
) -> StatusType:
    if not field_probed_and_available or total == 0:
        return "blocked"
    if truncated_or_incomplete:
        return "partial" if (resolved > 0 or conflicts > 0) else "blocked"
    if resolved == total and conflicts == 0:
        return "ready"
    if resolved > 0 or conflicts > 0:
        return "partial"
    return "blocked"


def evaluate_organizational_coverage(
    anonymized_users: Sequence[Dict[str, Any]],
    anonymized_deals: Optional[Sequence[Dict[str, Any]]] = None,
    probed_fields_summary: Optional[Dict[str, List[str]]] = None,
    circuit_broken: bool = False,
    period: Optional[Dict[str, Any]] = None,
    sources: Optional[Dict[str, Dict[str, Any]]] = None,
    completeness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Calculate aggregate organizational coverage metrics with strict role isolation and temporal semantics."""
    probed = probed_fields_summary or {"accepted": [], "rejected": []}
    accepted_fields = set(probed.get("accepted") or [])
    org_accepted_fields = {f for f in accepted_fields if f.lower() not in GENERIC_NON_ORG_FIELDS}

    users = list(anonymized_users)
    deals = list(anonymized_deals or [])

    # Source completeness tracking
    if sources is not None:
        sources_meta = sources
    else:
        user_trunc = bool(completeness and completeness.get("truncated", False))
        user_comp = not user_trunc and not circuit_broken
        sources_meta = {
            "users": {
                "requested": True,
                "successful": not circuit_broken,
                "complete": user_comp,
                "truncated": user_trunc,
                "error_code": "vista_circuit_broken" if circuit_broken else None,
                "pages_reported": completeness.get("pages_reported", 1) if completeness else 1,
                "pages_fetched": completeness.get("pages_fetched", 1) if completeness else 1,
                "records_fetched": completeness.get("records_fetched", len(users)) if completeness else len(users),
            },
            "deals": {
                "requested": bool(period),
                "successful": bool(period) and not circuit_broken,
                "complete": bool(period) and not circuit_broken,
                "truncated": False,
                "error_code": None,
                "pages_reported": 1 if period else 0,
                "pages_fetched": 1 if period else 0,
                "records_fetched": len(deals),
            },
        }

    any_source_failed = any(
        s.get("requested") and not s.get("successful") for s in sources_meta.values()
    )
    any_source_truncated = any(
        s.get("requested") and s.get("truncated") for s in sources_meta.values()
    )
    any_source_incomplete = any(
        s.get("requested") and not s.get("complete") for s in sources_meta.values()
    )

    # Strict role categorization: unknown NEVER treated as broker
    brokers: List[Dict[str, Any]] = []
    managers: List[Dict[str, Any]] = []
    directors: List[Dict[str, Any]] = []
    staff: List[Dict[str, Any]] = []
    unknown_roles: List[Dict[str, Any]] = []

    for u in users:
        role = u.get("role_type")
        if role == "broker":
            brokers.append(u)
        elif role == "manager":
            managers.append(u)
        elif role == "director":
            directors.append(u)
        elif role == "staff":
            staff.append(u)
        else:
            unknown_roles.append(u)

    # 1. Dimension: Broker -> Team (corretor -> equipe)
    # Only true brokers (role_type == 'broker') compose the current broker directory
    broker_ids = {b["user_id"] for b in brokers if b.get("user_id")}
    total_evaluated_brokers = len(broker_ids)

    user_broker_teams: Dict[str, Set[str]] = defaultdict(set)
    deal_broker_teams_by_date: Dict[str, Dict[Optional[str], Set[str]]] = defaultdict(lambda: defaultdict(set))
    broker_name_only: Set[str] = set()
    broker_active_count = 0
    broker_inactive_count = 0

    for b in brokers:
        uid = b.get("user_id")
        if not uid:
            continue
        if b.get("is_active", True):
            broker_active_count += 1
        else:
            broker_inactive_count += 1

        tid = b.get("team_id")
        if tid:
            user_broker_teams[uid].add(tid)
        elif b.get("team_name_only"):
            broker_name_only.add(uid)

    for d in deals:
        bid = d.get("broker_id")
        tid = d.get("team_id")
        ddate = d.get("deal_date")
        if bid and bid in broker_ids:
            if tid:
                deal_broker_teams_by_date[bid][ddate].add(tid)
            elif d.get("team_name_only") and bid not in user_broker_teams:
                broker_name_only.add(bid)

    brokers_resolved = 0
    brokers_conflict = 0
    brokers_historical_multi_team = 0

    for bid in broker_ids:
        curr_teams = user_broker_teams.get(bid, set())
        dated_teams = deal_broker_teams_by_date.get(bid, {})
        all_distinct_teams = curr_teams.copy()
        for ddate, dteams in dated_teams.items():
            all_distinct_teams.update(dteams)

        has_simultaneous_conflict = len(curr_teams) > 1 or any(
            len(dteams) > 1 for dteams in dated_teams.values()
        )

        if has_simultaneous_conflict:
            brokers_conflict += 1
        elif len(all_distinct_teams) == 1:
            brokers_resolved += 1
        elif len(all_distinct_teams) > 1:
            brokers_resolved += 1
            brokers_historical_multi_team += 1

    brokers_name_only_count = len(
        (broker_name_only & broker_ids) - set(user_broker_teams.keys()) - set(deal_broker_teams_by_date.keys())
    )
    brokers_unresolved = max(
        0, total_evaluated_brokers - brokers_resolved - brokers_conflict
    )

    # 2. Dimension: Team -> Manager (equipe -> gerente) with temporal semantics
    user_team_managers: Dict[str, Set[str]] = defaultdict(set)
    deal_team_managers_by_date: Dict[str, Dict[Optional[str], Set[str]]] = defaultdict(lambda: defaultdict(set))
    team_name_only_managers: Set[str] = set()

    for u in users:
        tid = u.get("team_id")
        if not tid:
            continue
        mid = u.get("manager_id")
        if mid:
            user_team_managers[tid].add(mid)
        elif u.get("manager_name_only"):
            team_name_only_managers.add(tid)

    for d in deals:
        tid = d.get("team_id")
        mid = d.get("manager_id")
        ddate = d.get("deal_date")
        if tid and mid:
            deal_team_managers_by_date[tid][ddate].add(mid)
        elif tid and d.get("manager_name_only") and tid not in user_team_managers:
            team_name_only_managers.add(tid)

    # 3. Dimension: Team -> Agency/Store (equipe -> agencia/loja) with temporal semantics
    user_team_agencies: Dict[str, Set[str]] = defaultdict(set)
    deal_team_agencies_by_date: Dict[str, Dict[Optional[str], Set[str]]] = defaultdict(lambda: defaultdict(set))
    team_name_only_agencies: Set[str] = set()

    for u in users:
        tid = u.get("team_id")
        if not tid:
            continue
        aid = u.get("agency_id")
        if aid:
            user_team_agencies[tid].add(aid)
        elif u.get("agency_name_only"):
            team_name_only_agencies.add(tid)

    for d in deals:
        tid = d.get("team_id")
        aid = d.get("agency_id")
        ddate = d.get("deal_date")
        if tid and aid:
            deal_team_agencies_by_date[tid][ddate].add(aid)
        elif tid and d.get("agency_name_only") and tid not in user_team_agencies:
            team_name_only_agencies.add(tid)

    all_team_ids = (
        set(user_team_managers.keys())
        | set(deal_team_managers_by_date.keys())
        | set(user_team_agencies.keys())
        | set(deal_team_agencies_by_date.keys())
        | {u["team_id"] for u in users if u.get("team_id")}
        | {d["team_id"] for d in deals if d.get("team_id")}
    )
    total_teams = len(all_team_ids)

    # Evaluate team->manager
    teams_resolved_manager = 0
    teams_conflict_manager = 0
    teams_historical_mgr_changes = 0

    for tid in all_team_ids:
        curr_mgrs = user_team_managers.get(tid, set())
        dated_mgrs = deal_team_managers_by_date.get(tid, {})
        all_distinct_mgrs = curr_mgrs.copy()
        for ddate, dmgrs in dated_mgrs.items():
            all_distinct_mgrs.update(dmgrs)

        has_simultaneous_conflict = len(curr_mgrs) > 1 or any(
            len(dmgrs) > 1 for dmgrs in dated_mgrs.values()
        )

        if has_simultaneous_conflict:
            teams_conflict_manager += 1
        elif len(all_distinct_mgrs) == 1:
            teams_resolved_manager += 1
        elif len(all_distinct_mgrs) > 1:
            teams_resolved_manager += 1
            teams_historical_mgr_changes += 1

    teams_name_only_mgr_count = len(
        team_name_only_managers - set(user_team_managers.keys()) - set(deal_team_managers_by_date.keys())
    )
    teams_unresolved_manager = max(
        0, total_teams - teams_resolved_manager - teams_conflict_manager
    )

    # Evaluate manager->agency
    manager_ids = {m["user_id"] for m in (managers + directors) if m.get("user_id")}
    total_managers = len(manager_ids)

    manager_agencies: Dict[str, Set[str]] = defaultdict(set)
    deal_manager_agencies_by_date: Dict[str, Dict[Optional[str], Set[str]]] = defaultdict(lambda: defaultdict(set))
    manager_name_only_agencies: Set[str] = set()
    manager_active_count = 0
    manager_inactive_count = 0

    for m in (managers + directors):
        uid = m.get("user_id")
        if not uid:
            continue
        if m.get("is_active", True):
            manager_active_count += 1
        else:
            manager_inactive_count += 1

        aid = m.get("agency_id")
        if aid:
            manager_agencies[uid].add(aid)
        elif m.get("agency_name_only"):
            manager_name_only_agencies.add(uid)

    for d in deals:
        mid = d.get("manager_id")
        aid = d.get("agency_id")
        ddate = d.get("deal_date")
        if mid and mid in manager_ids:
            if aid:
                deal_manager_agencies_by_date[mid][ddate].add(aid)
            elif d.get("agency_name_only") and mid not in manager_agencies:
                manager_name_only_agencies.add(mid)

    managers_resolved_agency = 0
    managers_conflict_agency = 0
    managers_historical_agency_changes = 0

    for mid in manager_ids:
        curr_agencies = manager_agencies.get(mid, set())
        dated_agencies = deal_manager_agencies_by_date.get(mid, {})
        all_distinct_agencies = curr_agencies.copy()
        for ddate, dagencies in dated_agencies.items():
            all_distinct_agencies.update(dagencies)

        has_simultaneous_conflict = len(curr_agencies) > 1 or any(
            len(dagencies) > 1 for dagencies in dated_agencies.values()
        )

        if has_simultaneous_conflict:
            managers_conflict_agency += 1
        elif len(all_distinct_agencies) == 1:
            managers_resolved_agency += 1
        elif len(all_distinct_agencies) > 1:
            managers_resolved_agency += 1
            managers_historical_agency_changes += 1

    managers_name_only_agency_count = len(
        (manager_name_only_agencies & manager_ids) - set(manager_agencies.keys()) - set(deal_manager_agencies_by_date.keys())
    )
    managers_unresolved_agency = max(
        0, total_managers - managers_resolved_agency - managers_conflict_agency
    )

    # Evaluate team->agency with temporal semantics
    teams_resolved_agency = 0
    teams_conflict_agency = 0
    teams_historical_agency_changes = 0

    for tid in all_team_ids:
        curr_agencies = user_team_agencies.get(tid, set())
        dated_agencies = deal_team_agencies_by_date.get(tid, {})
        all_distinct_agencies = curr_agencies.copy()
        for ddate, dagencies in dated_agencies.items():
            all_distinct_agencies.update(dagencies)

        has_simultaneous_conflict = len(curr_agencies) > 1 or any(
            len(dagencies) > 1 for dagencies in dated_agencies.values()
        )

        if has_simultaneous_conflict:
            teams_conflict_agency += 1
        elif len(all_distinct_agencies) == 1:
            teams_resolved_agency += 1
        elif len(all_distinct_agencies) > 1:
            teams_resolved_agency += 1
            teams_historical_agency_changes += 1

    teams_name_only_agency_count = len(
        team_name_only_agencies - set(user_team_agencies.keys()) - set(deal_team_agencies_by_date.keys())
    )
    teams_unresolved_agency = max(
        0, total_teams - teams_resolved_agency - teams_conflict_agency
    )

    # Support and availability resolution per dimension
    if not accepted_fields:
        broker_to_team_supported = False
        team_to_manager_supported = False
        manager_to_agency_supported = False
        team_to_agency_supported = False
        overall_supported = False
    else:
        has_team_field = any("equipe" in f.lower() or "team" in f.lower() for f in org_accepted_fields)
        has_broker_team_evidence = bool(
            brokers_resolved > 0
            or any(b.get("team_id") for b in brokers)
            or any(d.get("team_id") for d in deals if d.get("broker_id") in broker_ids)
        )
        broker_to_team_supported = bool(
            has_broker_team_evidence
            or has_team_field
            or (org_accepted_fields and not any("gerente" in f.lower() or "manager" in f.lower() or "agencia" in f.lower() or "loja" in f.lower() or "agency" in f.lower() or "store" in f.lower() for f in org_accepted_fields))
        )

        has_manager_field = any("gerente" in f.lower() or "gestor" in f.lower() or "manager" in f.lower() for f in org_accepted_fields)
        has_team_mgr_evidence = bool(
            teams_resolved_manager > 0
            or any(u.get("manager_id") for u in users)
            or any(d.get("manager_id") for d in deals)
        )
        team_to_manager_supported = bool(
            has_team_mgr_evidence
            or has_manager_field
            or (org_accepted_fields and not any("equipe" in f.lower() or "team" in f.lower() or "agencia" in f.lower() or "loja" in f.lower() for f in org_accepted_fields))
        )

        has_agency_field = any("agencia" in f.lower() or "loja" in f.lower() or "agency" in f.lower() or "store" in f.lower() or "filial" in f.lower() for f in org_accepted_fields)
        has_mgr_agency_evidence = bool(
            managers_resolved_agency > 0
            or any(m.get("agency_id") for m in (managers + directors))
            or any(d.get("agency_id") for d in deals if d.get("manager_id") in manager_ids)
        )
        manager_to_agency_supported = bool(
            has_mgr_agency_evidence
            or has_agency_field
            or (org_accepted_fields and not any("equipe" in f.lower() or "team" in f.lower() or "gerente" in f.lower() or "gestor" in f.lower() for f in org_accepted_fields))
        )

        has_team_agency_evidence = bool(
            teams_resolved_agency > 0
            or any(u.get("agency_id") for u in users)
            or any(d.get("agency_id") for d in deals)
        )
        team_to_agency_supported = bool(
            has_team_agency_evidence
            or has_agency_field
            or (org_accepted_fields and not any("equipe" in f.lower() or "team" in f.lower() or "gerente" in f.lower() or "gestor" in f.lower() for f in org_accepted_fields))
        )

        overall_supported = bool(
            broker_to_team_supported
            or team_to_manager_supported
            or manager_to_agency_supported
            or team_to_agency_supported
        )

    broker_team_available = bool(broker_to_team_supported or (overall_supported and brokers_resolved > 0))
    team_manager_available = bool(team_to_manager_supported or (overall_supported and teams_resolved_manager > 0))
    manager_agency_available = bool(manager_to_agency_supported or (overall_supported and managers_resolved_agency > 0))
    team_agency_available = bool(team_to_agency_supported or (overall_supported and teams_resolved_agency > 0))

    is_truncated_or_incomplete = (
        any_source_failed or any_source_truncated or any_source_incomplete or not overall_supported
    )
    overall_complete = (
        overall_supported
        and not circuit_broken
        and not any_source_failed
        and not any_source_truncated
        and not any_source_incomplete
    )

    combined_completeness = {
        "complete": overall_complete,
        "truncated": any_source_truncated,
        "supported": overall_supported,
        "pages_reported": sum(
            s.get("pages_reported", 0) for s in sources_meta.values() if s.get("requested")
        ),
        "pages_fetched": sum(
            s.get("pages_fetched", 0) for s in sources_meta.values() if s.get("requested")
        ),
        "records_fetched": sum(
            s.get("records_fetched", 0) for s in sources_meta.values() if s.get("requested")
        ),
    }

    broker_to_team_status = compute_dimension_status(
        total_evaluated_brokers,
        brokers_resolved,
        brokers_conflict,
        broker_team_available,
        truncated_or_incomplete=is_truncated_or_incomplete,
    )
    broker_to_team_ratio = (
        round(brokers_resolved / total_evaluated_brokers, 4)
        if total_evaluated_brokers > 0
        else 0.0
    )

    team_to_manager_status = compute_dimension_status(
        total_teams,
        teams_resolved_manager,
        teams_conflict_manager,
        team_manager_available,
        truncated_or_incomplete=is_truncated_or_incomplete,
    )
    team_to_manager_ratio = (
        round(teams_resolved_manager / total_teams, 4) if total_teams > 0 else 0.0
    )

    manager_to_agency_status = compute_dimension_status(
        total_managers,
        managers_resolved_agency,
        managers_conflict_agency,
        manager_agency_available,
        truncated_or_incomplete=is_truncated_or_incomplete,
    )
    manager_to_agency_ratio = (
        round(managers_resolved_agency / total_managers, 4)
        if total_managers > 0
        else 0.0
    )

    team_to_agency_status = compute_dimension_status(
        total_teams,
        teams_resolved_agency,
        teams_conflict_agency,
        team_agency_available,
        truncated_or_incomplete=is_truncated_or_incomplete,
    )
    team_to_agency_ratio = (
        round(teams_resolved_agency / total_teams, 4) if total_teams > 0 else 0.0
    )

    # Overall Status Calculation
    dimension_statuses = [
        broker_to_team_status,
        team_to_manager_status,
        manager_to_agency_status,
        team_to_agency_status,
    ]

    if circuit_broken or not overall_supported or all(s == "blocked" for s in dimension_statuses):
        overall_status: StatusType = "blocked"
    elif is_truncated_or_incomplete:
        overall_status = "partial" if any(s in ("partial", "ready") for s in dimension_statuses) else "blocked"
    elif all(s == "ready" for s in dimension_statuses):
        overall_status = "ready"
    else:
        overall_status = "partial"

    # Blocks and diagnostics
    blocks_found: List[str] = []
    if circuit_broken:
        blocks_found.append("circuit_breaker_triggered_after_two_failures")
    if not overall_supported:
        blocks_found.append("no_supported_fields_negotiated")
    if any_source_failed:
        blocks_found.append("one_or_more_requested_sources_failed")
    if any_source_truncated:
        blocks_found.append("snapshot_truncated_by_pagination_limit")
    if any_source_incomplete and not any_source_truncated and not any_source_failed:
        blocks_found.append("one_or_more_requested_sources_incomplete")

    if broker_to_team_status == "blocked":
        blocks_found.append("broker_to_team_unsupported_or_missing_ids")
    elif brokers_conflict > 0:
        blocks_found.append("broker_to_team_contains_simultaneous_conflicts")
    elif brokers_name_only_count > 0:
        blocks_found.append("broker_to_team_contains_name_only_unverified_records")

    if team_to_manager_status == "blocked":
        blocks_found.append("team_to_manager_unsupported_or_missing_ids")
    elif teams_conflict_manager > 0:
        blocks_found.append("team_to_manager_contains_simultaneous_conflicts")
    elif teams_name_only_mgr_count > 0:
        blocks_found.append("team_to_manager_contains_name_only_unverified_records")

    if manager_to_agency_status == "blocked":
        blocks_found.append("manager_to_agency_unsupported_or_missing_ids")
    elif managers_conflict_agency > 0:
        blocks_found.append("manager_to_agency_contains_simultaneous_conflicts")
    elif managers_name_only_agency_count > 0:
        blocks_found.append("manager_to_agency_contains_name_only_unverified_records")

    if team_to_agency_status == "blocked":
        blocks_found.append("team_to_agency_unsupported_or_missing_ids")
    elif teams_conflict_agency > 0:
        blocks_found.append("team_to_agency_contains_simultaneous_conflicts")
    elif teams_name_only_agency_count > 0:
        blocks_found.append("team_to_agency_contains_name_only_unverified_records")

    return {
        "contract_version": "1.0",
        "diagnostic_target": "vista_organizational_stable_id_coverage",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "period": period,
        "sources": sources_meta,
        "completeness": combined_completeness,
        "semantics": {
            "snapshot_type": "point_in_time_cohort",
            "event_history_preserved": False,
            "warning": (
                "Vista REST APIs provide point-in-time snapshot and bounded period cohorts. "
                "Historical stage transitions and assignment history require dedicated event logs."
            ),
        },
        "overall_status": overall_status,
        "circuit_breaker": {
            "triggered": circuit_broken,
            "max_failure_threshold": 2,
        },
        "role_summary": {
            "total_users_evaluated": len(users),
            "brokers_count": len(brokers),
            "managers_count": len(managers),
            "directors_count": len(directors),
            "staff_count": len(staff),
            "unknown_role_count": len(unknown_roles),
        },
        "field_negotiation": {
            "supported": overall_supported,
            "accepted_fields": sorted(accepted_fields),
            "rejected_fields": sorted(set(probed.get("rejected") or [])),
        },
        "dimensions": {
            "broker_to_team": {
                "dimension_name": "corretor_para_equipe",
                "supported": broker_to_team_supported,
                "status": broker_to_team_status,
                "coverage_ratio": broker_to_team_ratio,
                "coverage_percentage": round(broker_to_team_ratio * 100, 2),
                "metrics": {
                    "total_brokers": total_evaluated_brokers,
                    "resolved": brokers_resolved,
                    "conflict": brokers_conflict,
                    "unresolved": brokers_unresolved,
                    "name_only_unverified": brokers_name_only_count,
                    "historical_multi_team_evidence": brokers_historical_multi_team,
                },
                "status_counts": {
                    "active": broker_active_count,
                    "inactive": broker_inactive_count,
                },
            },
            "team_to_manager": {
                "dimension_name": "equipe_para_gerente",
                "supported": team_to_manager_supported,
                "status": team_to_manager_status,
                "coverage_ratio": team_to_manager_ratio,
                "coverage_percentage": round(team_to_manager_ratio * 100, 2),
                "metrics": {
                    "total_teams": total_teams,
                    "resolved": teams_resolved_manager,
                    "conflict": teams_conflict_manager,
                    "unresolved": teams_unresolved_manager,
                    "name_only_unverified": teams_name_only_mgr_count,
                    "historical_change_evidence": teams_historical_mgr_changes,
                },
            },
            "manager_to_agency": {
                "dimension_name": "gerente_para_agencia",
                "supported": manager_to_agency_supported,
                "status": manager_to_agency_status,
                "coverage_ratio": manager_to_agency_ratio,
                "coverage_percentage": round(manager_to_agency_ratio * 100, 2),
                "metrics": {
                    "total_managers": total_managers,
                    "resolved": managers_resolved_agency,
                    "conflict": managers_conflict_agency,
                    "unresolved": managers_unresolved_agency,
                    "name_only_unverified": managers_name_only_agency_count,
                    "historical_change_evidence": managers_historical_agency_changes,
                },
                "status_counts": {
                    "active": manager_active_count,
                    "inactive": manager_inactive_count,
                },
            },
            "team_to_agency": {
                "dimension_name": "equipe_para_agencia",
                "supported": team_to_agency_supported,
                "status": team_to_agency_status,
                "coverage_ratio": team_to_agency_ratio,
                "coverage_percentage": round(team_to_agency_ratio * 100, 2),
                "metrics": {
                    "total_teams": total_teams,
                    "resolved": teams_resolved_agency,
                    "conflict": teams_conflict_agency,
                    "unresolved": teams_unresolved_agency,
                    "name_only_unverified": teams_name_only_agency_count,
                    "historical_change_evidence": teams_historical_agency_changes,
                },
            },
        },
        "blocks_found": blocks_found,
        "privacy_guarantee": {
            "personal_names_included": False,
            "emails_included": False,
            "credentials_included": False,
            "raw_payloads_included": False,
            "only_aggregate_counts": True,
        },
    }
