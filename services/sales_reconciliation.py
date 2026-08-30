"""Business rule for Pipeimob + Vista consolidated sales."""

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, DefaultDict, Dict, List, Literal, Optional, Sequence


MATCHED = "CONCILIADO"
PIPE_WITHOUT_GAIN = "PIPEIMOB_SEM_GANHO_VISTA"
VISTA_WITHOUT_CONTRACT = "VISTA_SEM_CONTRATO_PIPEIMOB"
VALUE_MISMATCH = "DIVERGENCIA_VALOR"
DATE_MISMATCH = "DIVERGENCIA_DATA"
NO_LINK = "SEM_VINCULO_AUTOMATICO"
SOURCE_DATA_INCOMPLETE = "DADO_FONTE_INCOMPLETO"

PIPEIMOB_OFFICIAL_SALE_DATE_FIELDS = (
    "data_assinatura_ccv",
    "data_ccv",
    "data_assinatura",
    "data_contrato",
)


def pipeimob_official_sale_date(row: Dict[str, Any]) -> Any:
    """Return Pipeimob's official sale date without using process-start dates."""
    for field in PIPEIMOB_OFFICIAL_SALE_DATE_FIELDS:
        value = row.get(field)
        if value not in (None, ""):
            return value
    return None


def reconcile_sales(
    pipeimob_transactions: Sequence[Dict[str, Any]],
    vista_gains: Sequence[Dict[str, Any]],
    date_tolerance_days: int = 7,
    pipeimob_group_mapping: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return official Pipeimob totals enriched by Vista commercial ownership."""
    if date_tolerance_days < 0:
        raise ValueError("date_tolerance_days cannot be negative")

    pipe_sales = [
        _normalize_pipe_sale(row, pipeimob_group_mapping or {})
        for row in pipeimob_transactions
    ]
    gains = [_normalize_vista_gain(row) for row in vista_gains]
    _assert_unique(pipe_sales, "transaction_id")
    _assert_unique(gains, "deal_id")

    gains_by_property: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    for gain in gains:
        if gain["property_code"]:
            gains_by_property[gain["property_code"]].append(gain)

    used_deal_ids = set()
    items: List[Dict[str, Any]] = []

    for sale in pipe_sales:
        missing = [
            field
            for field in ("transaction_id", "property_code", "official_date", "official_value")
            if sale[field] in (None, "")
        ]
        if missing:
            items.append(
                _base_pipe_item(
                    sale,
                    SOURCE_DATA_INCOMPLETE,
                    issues=[SOURCE_DATA_INCOMPLETE],
                    missing_fields=missing,
                )
            )
            continue

        candidates = [
            gain
            for gain in gains_by_property[sale["property_code"]]
            if gain["deal_id"] not in used_deal_ids
        ]
        selected = _select_candidate(sale, candidates)
        if selected is None:
            status = NO_LINK if candidates else PIPE_WITHOUT_GAIN
            items.append(_base_pipe_item(sale, status))
            continue

        used_deal_ids.add(selected["deal_id"])
        issues: List[str] = []
        value_difference = (
            selected["deal_value"] - sale["official_value"]
            if selected["deal_value"] is not None
            else None
        )
        delay_days = (
            (selected["gain_date"] - sale["official_date"]).days
            if selected["gain_date"] is not None
            else None
        )
        if value_difference is not None and value_difference != Decimal("0"):
            issues.append(VALUE_MISMATCH)
        if delay_days is not None and abs(delay_days) > date_tolerance_days:
            issues.append(DATE_MISMATCH)

        fiscal_broker = sale["fiscal_broker"]
        commercial_broker = selected["commercial_broker_name"]
        broker_roles_differ = None
        if fiscal_broker and commercial_broker:
            broker_roles_differ = _normalize_text(fiscal_broker) != _normalize_text(
                commercial_broker
            )

        team_resolution = _resolve_api_team(sale, selected)
        items.append(
            {
                **_base_pipe_item(sale, issues[0] if issues else MATCHED),
                "issues": issues,
                "vista_deal_id": selected["deal_id"],
                "vista_gain_date": (
                    selected["gain_date"].isoformat()
                    if selected["gain_date"] is not None
                    else None
                ),
                "delay_days": delay_days,
                "vista_value": (
                    str(selected["deal_value"])
                    if selected["deal_value"] is not None
                    else None
                ),
                "value_difference": (
                    str(value_difference) if value_difference is not None else None
                ),
                "commercial_broker_id": selected["commercial_broker_id"],
                "commercial_broker": commercial_broker,
                "broker_roles_differ": broker_roles_differ,
                "vista_stage": selected["stage_name"],
                **team_resolution,
            }
        )

    for gain in gains:
        if gain["deal_id"] and gain["deal_id"] not in used_deal_ids:
            items.append(
                {
                    "status": VISTA_WITHOUT_CONTRACT,
                    "issues": [],
                    "pipeimob_transaction_id": None,
                    "vista_deal_id": gain["deal_id"],
                    "property_code": gain["property_code"],
                    "official_sale_date": None,
                    "vista_gain_date": (
                        gain["gain_date"].isoformat() if gain["gain_date"] else None
                    ),
                    "official_value": None,
                    "vista_value": (
                        str(gain["deal_value"])
                        if gain["deal_value"] is not None
                        else None
                    ),
                    "commercial_broker_id": gain["commercial_broker_id"],
                    "commercial_broker": gain["commercial_broker_name"],
                    "fiscal_broker": None,
                    "responsible_manager": None,
                    "team_name": gain["commercial_team_name"],
                    "team_source": (
                        "vista_deal" if gain["commercial_team_name"] else None
                    ),
                    "team_resolution_status": (
                        "resolved" if gain["commercial_team_name"] else "unresolved"
                    ),
                    "vista_team": gain["commercial_team_name"],
                    "pipeimob_team": None,
                }
            )

    status_counts = Counter(item["status"] for item in items)
    issue_counts = Counter(issue for item in items for issue in item["issues"])
    official_vgv = sum(
        (
            sale["official_value"]
            for sale in pipe_sales
            if sale["official_value"] is not None
        ),
        Decimal("0"),
    )
    missing_commercial_broker = sum(
        1
        for item in items
        if item.get("vista_deal_id") and not item.get("commercial_broker")
    )
    official_items = [item for item in items if item.get("pipeimob_transaction_id")]
    api_team_resolved = sum(1 for item in official_items if item.get("team_name"))
    api_team_conflicts = sum(
        1
        for item in official_items
        if item.get("team_resolution_status") == "conflict_api_sources"
    )
    ambiguous_pipeimob_team = sum(
        1
        for item in official_items
        if item.get("team_resolution_status") == "ambiguous_pipeimob_groups"
    )

    return {
        "contract_version": "1.1",
        "official_source": "pipeimob_api_v2",
        "commercial_source": "vista_negocio_ganho",
        "summary": {
            "official_sales": len(pipe_sales),
            "official_vgv": str(official_vgv),
            "matched": status_counts[MATCHED],
            "pipeimob_without_vista_gain": status_counts[PIPE_WITHOUT_GAIN],
            "vista_without_pipeimob_contract": status_counts[VISTA_WITHOUT_CONTRACT],
            "value_mismatches": issue_counts[VALUE_MISMATCH],
            "date_mismatches": issue_counts[DATE_MISMATCH],
            "no_automatic_link": status_counts[NO_LINK],
            "source_data_incomplete": status_counts[SOURCE_DATA_INCOMPLETE],
            "missing_commercial_broker": missing_commercial_broker,
            "api_team_resolved": api_team_resolved,
            "api_team_unresolved": max(len(pipe_sales) - api_team_resolved, 0),
            "api_team_conflicts": api_team_conflicts,
            "ambiguous_pipeimob_team": ambiguous_pipeimob_team,
        },
        "items": items,
    }


def rank_commercial_sales(
    reconciliation: Dict[str, Any],
    metric: Literal["sales_count", "vgv"] = "sales_count",
) -> Dict[str, Any]:
    """Aggregate official Pipeimob sales by Vista's commercial broker.

    Only records backed by an official Pipeimob transaction are counted. A
    linked sale remains attributable when its date or value differs between
    systems; those differences are reconciliation issues, not evidence that
    the signed contract stopped being an official sale.
    """
    if metric not in ("sales_count", "vgv"):
        raise ValueError("metric must be sales_count or vgv")

    grouped: Dict[str, Dict[str, Any]] = {}
    attributed_sales = 0
    attributed_vgv = Decimal("0")

    for item in reconciliation.get("items") or []:
        if not isinstance(item, dict) or not item.get("pipeimob_transaction_id"):
            continue

        broker = _clean(item.get("commercial_broker"))
        if not broker:
            continue

        normalized = _normalize_text(broker)
        row = grouped.setdefault(
            normalized,
            {
                "commercial_broker": broker,
                "sales_count": 0,
                "vgv": Decimal("0"),
            },
        )
        value = _parse_decimal(item.get("official_value")) or Decimal("0")
        row["sales_count"] += 1
        row["vgv"] += value
        attributed_sales += 1
        attributed_vgv += value

    ranking = []
    for row in grouped.values():
        sales_count = int(row["sales_count"])
        vgv = row["vgv"]
        ranking.append(
            {
                "commercial_broker": row["commercial_broker"],
                "sales_count": sales_count,
                "vgv": str(vgv),
                "average_ticket": str(
                    vgv / Decimal(sales_count) if sales_count else Decimal("0")
                ),
            }
        )

    if metric == "vgv":
        ranking.sort(
            key=lambda row: (
                -Decimal(row["vgv"]),
                -row["sales_count"],
                _normalize_text(row["commercial_broker"]),
            )
        )
    else:
        ranking.sort(
            key=lambda row: (
                -row["sales_count"],
                -Decimal(row["vgv"]),
                _normalize_text(row["commercial_broker"]),
            )
        )

    for position, row in enumerate(ranking, start=1):
        row["position"] = position

    summary = reconciliation.get("summary") or {}
    official_sales = int(summary.get("official_sales") or 0)
    official_vgv = _parse_decimal(summary.get("official_vgv")) or Decimal("0")
    return {
        "contract_version": "1.0",
        "official_source": reconciliation.get("official_source"),
        "commercial_source": reconciliation.get("commercial_source"),
        "attribution": "vista_commercial_broker",
        "metric": metric,
        "period": reconciliation.get("period"),
        "generated_at": reconciliation.get("generated_at"),
        "summary": {
            "official_sales": official_sales,
            "official_vgv": str(official_vgv),
            "attributed_sales": attributed_sales,
            "attributed_vgv": str(attributed_vgv),
            "unattributed_sales": max(official_sales - attributed_sales, 0),
            "unattributed_vgv": str(max(official_vgv - attributed_vgv, Decimal("0"))),
        },
        "ranking": ranking,
    }


def _normalize_pipe_sale(
    row: Dict[str, Any],
    group_mapping: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    responsible_manager = _clean(row.get("agente_gestor"))
    pipeimob_team = _resolve_pipeimob_group_team(row, group_mapping)
    return {
        "transaction_id": _clean(row.get("transacao_unique_id_pipeimob")),
        "contract_code": _clean(row.get("codigo_contrato")),
        "property_code": _normalize_code(row.get("codigo_imovel")),
        "official_date": _parse_date(pipeimob_official_sale_date(row)),
        "official_value": _parse_decimal(row.get("valor_contrato")),
        # Kept for backwards compatibility. Pipeimob's agente_gestor is the
        # transaction's responsible manager, not the commercial broker.
        "fiscal_broker": responsible_manager,
        "responsible_manager": responsible_manager,
        **pipeimob_team,
    }


def _normalize_vista_gain(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "deal_id": _clean(row.get("deal_id")),
        "property_code": _normalize_code(row.get("property_code")),
        "gain_date": _parse_date(row.get("gain_date")),
        "deal_value": _parse_decimal(row.get("deal_value")),
        "commercial_broker_id": _clean(row.get("commercial_broker_id")),
        "commercial_broker_name": _clean(row.get("commercial_broker_name")),
        "commercial_team_name": _clean(row.get("commercial_team_name")),
        "stage_name": _clean(row.get("stage_name")),
    }


def _resolve_api_team(
    sale: Dict[str, Any], gain: Dict[str, Any]
) -> Dict[str, Any]:
    """Prefer transaction-specific API evidence and expose any disagreement."""
    vista_team = gain.get("commercial_team_name")
    pipeimob_team = sale.get("pipeimob_team_name")
    pipeimob_status = sale.get("pipeimob_team_status")

    if vista_team:
        conflict = bool(
            pipeimob_team
            and _normalize_text(str(vista_team))
            != _normalize_text(str(pipeimob_team))
        )
        return {
            "team_name": vista_team,
            "team_source": "vista_deal",
            "team_resolution_status": (
                "conflict_api_sources" if conflict else "resolved"
            ),
            "vista_team": vista_team,
            "pipeimob_team": pipeimob_team,
        }

    if pipeimob_team:
        return {
            "team_name": pipeimob_team,
            "team_source": "pipeimob_responsible_group",
            "team_resolution_status": "resolved",
            "vista_team": None,
            "pipeimob_team": pipeimob_team,
        }

    return {
        "team_name": None,
        "team_source": None,
        "team_resolution_status": (
            "ambiguous_pipeimob_groups"
            if pipeimob_status == "ambiguous"
            else "unresolved"
        ),
        "vista_team": None,
        "pipeimob_team": None,
    }


def _resolve_pipeimob_group_team(
    row: Dict[str, Any], group_mapping: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    group_ids = _extract_pipeimob_group_ids(row)
    teams: Dict[str, str] = {}
    for group_id in group_ids:
        configured = group_mapping.get(group_id)
        if not isinstance(configured, dict) or configured.get("type") != "team":
            continue
        name = _clean(configured.get("name"))
        if name:
            teams[_normalize_text(name)] = name

    if len(teams) == 1:
        return {
            "pipeimob_group_ids": group_ids,
            "pipeimob_team_name": next(iter(teams.values())),
            "pipeimob_team_status": "resolved",
        }
    return {
        "pipeimob_group_ids": group_ids,
        "pipeimob_team_name": None,
        "pipeimob_team_status": "ambiguous" if len(teams) > 1 else "unresolved",
    }


def _extract_pipeimob_group_ids(row: Dict[str, Any]) -> List[str]:
    values: List[Any] = []
    primary = row.get("agente_gestor_grupos_a_que_pertence")
    if isinstance(primary, (list, tuple, set)):
        values.extend(primary)
    elif primary not in (None, ""):
        values.append(primary)

    for field in (
        "agente_gestor_grupos_a_que_pertence1",
        "agente_gestor_grupos_a_que_pertence2",
        "agente_gestor_grupos_a_que_pertence3",
    ):
        value = row.get(field)
        if value not in (None, ""):
            values.append(value)

    result: List[str] = []
    seen = set()
    for value in values:
        group_id = _clean(value)
        if group_id and group_id not in seen:
            seen.add(group_id)
            result.append(group_id)
    return result


def _select_candidate(
    sale: Dict[str, Any], candidates: Sequence[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    # An exact, unique property-code match is enough to enrich the official
    # Pipeimob sale with Vista's commercial owner.  Date and VGV remain
    # authoritative in Pipeimob and may legitimately be absent from Vista's
    # negocios/listar response for this tenant.
    if len(candidates) == 1:
        return candidates[0]

    valid = [
        gain
        for gain in candidates
        if gain["gain_date"] is not None and gain["deal_value"] is not None
    ]
    if not valid:
        return None

    exact_value = [
        gain for gain in valid if gain["deal_value"] == sale["official_value"]
    ]
    pool = exact_value or valid
    nearest_distance = min(
        abs((gain["gain_date"] - sale["official_date"]).days) for gain in pool
    )
    nearest = [
        gain
        for gain in pool
        if abs((gain["gain_date"] - sale["official_date"]).days)
        == nearest_distance
    ]
    return nearest[0] if len(nearest) == 1 else None


def _base_pipe_item(
    sale: Dict[str, Any],
    status: str,
    issues: Optional[List[str]] = None,
    missing_fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "issues": issues or [],
        "missing_fields": missing_fields or [],
        "pipeimob_transaction_id": sale["transaction_id"],
        "pipeimob_contract_code": sale["contract_code"],
        "vista_deal_id": None,
        "property_code": sale["property_code"],
        "official_sale_date": (
            sale["official_date"].isoformat() if sale["official_date"] else None
        ),
        "vista_gain_date": None,
        "official_value": (
            str(sale["official_value"])
            if sale["official_value"] is not None
            else None
        ),
        "vista_value": None,
        "fiscal_broker": sale["fiscal_broker"],
        "responsible_manager": sale["responsible_manager"],
        "commercial_broker": None,
        "team_name": sale["pipeimob_team_name"],
        "team_source": (
            "pipeimob_responsible_group" if sale["pipeimob_team_name"] else None
        ),
        "team_resolution_status": (
            "resolved"
            if sale["pipeimob_team_name"]
            else (
                "ambiguous_pipeimob_groups"
                if sale["pipeimob_team_status"] == "ambiguous"
                else "unresolved"
            )
        ),
        "vista_team": None,
        "pipeimob_team": sale["pipeimob_team_name"],
    }


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("R$", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_code(value: Any) -> Optional[str]:
    text = _clean(value)
    return text.upper() if text else None


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _assert_unique(rows: Sequence[Dict[str, Any]], field: str) -> None:
    values = [row[field] for row in rows if row[field]]
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate source identifiers in {field}")
