"""Business rule for Pipeimob + Vista consolidated sales."""

from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, DefaultDict, Dict, List, Optional, Sequence


MATCHED = "CONCILIADO"
PIPE_WITHOUT_GAIN = "PIPEIMOB_SEM_GANHO_VISTA"
VISTA_WITHOUT_CONTRACT = "VISTA_SEM_CONTRATO_PIPEIMOB"
VALUE_MISMATCH = "DIVERGENCIA_VALOR"
DATE_MISMATCH = "DIVERGENCIA_DATA"
NO_LINK = "SEM_VINCULO_AUTOMATICO"
SOURCE_DATA_INCOMPLETE = "DADO_FONTE_INCOMPLETO"


def reconcile_sales(
    pipeimob_transactions: Sequence[Dict[str, Any]],
    vista_gains: Sequence[Dict[str, Any]],
    date_tolerance_days: int = 7,
) -> Dict[str, Any]:
    """Return official Pipeimob totals enriched by Vista commercial ownership."""
    if date_tolerance_days < 0:
        raise ValueError("date_tolerance_days cannot be negative")

    pipe_sales = [_normalize_pipe_sale(row) for row in pipeimob_transactions]
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
        value_difference = selected["deal_value"] - sale["official_value"]
        delay_days = (selected["gain_date"] - sale["official_date"]).days
        if value_difference != Decimal("0"):
            issues.append(VALUE_MISMATCH)
        if abs(delay_days) > date_tolerance_days:
            issues.append(DATE_MISMATCH)

        fiscal_broker = sale["fiscal_broker"]
        commercial_broker = selected["commercial_broker_name"]
        broker_roles_differ = None
        if fiscal_broker and commercial_broker:
            broker_roles_differ = _normalize_text(fiscal_broker) != _normalize_text(
                commercial_broker
            )

        items.append(
            {
                **_base_pipe_item(sale, issues[0] if issues else MATCHED),
                "issues": issues,
                "vista_deal_id": selected["deal_id"],
                "vista_gain_date": selected["gain_date"].isoformat(),
                "delay_days": delay_days,
                "vista_value": str(selected["deal_value"]),
                "value_difference": str(value_difference),
                "commercial_broker_id": selected["commercial_broker_id"],
                "commercial_broker": commercial_broker,
                "broker_roles_differ": broker_roles_differ,
                "vista_stage": selected["stage_name"],
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

    return {
        "contract_version": "1.0",
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
        },
        "items": items,
    }


def _normalize_pipe_sale(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "transaction_id": _clean(row.get("transacao_unique_id_pipeimob")),
        "contract_code": _clean(row.get("codigo_contrato")),
        "property_code": _normalize_code(row.get("codigo_imovel")),
        "official_date": _parse_date(row.get("data_assinatura_ccv")),
        "official_value": _parse_decimal(row.get("valor_contrato")),
        "fiscal_broker": _clean(row.get("agente_gestor")),
    }


def _normalize_vista_gain(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "deal_id": _clean(row.get("deal_id")),
        "property_code": _normalize_code(row.get("property_code")),
        "gain_date": _parse_date(row.get("gain_date")),
        "deal_value": _parse_decimal(row.get("deal_value")),
        "commercial_broker_id": _clean(row.get("commercial_broker_id")),
        "commercial_broker_name": _clean(row.get("commercial_broker_name")),
        "stage_name": _clean(row.get("stage_name")),
    }


def _select_candidate(
    sale: Dict[str, Any], candidates: Sequence[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    valid = [
        gain
        for gain in candidates
        if gain["gain_date"] is not None and gain["deal_value"] is not None
    ]
    if len(valid) == 1:
        return valid[0]
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
        "commercial_broker": None,
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
