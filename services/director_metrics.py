"""Read-only, PII-minimized views used by the directors' MCP server."""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List

import httpx


class DirectorMetricsError(RuntimeError):
    """Safe error raised when the protected BI backend is unavailable."""


class DirectorMetricsClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 35,
        sales_url: str | None = None,
    ) -> None:
        if not str(base_url or "").strip():
            raise ValueError("PIPEIMOB_BI_BACKEND_URL is required")
        self.base_url = base_url.rstrip("/")
        self.sales_url = str(sales_url or "").strip() or None
        self.timeout_seconds = max(1, min(60, int(timeout_seconds)))

    async def sales_reconciliation(
        self, start: str, end: str, bearer_token: str
    ) -> Dict[str, Any]:
        _validate_period(start, end)
        return await self._get_url(
            self.sales_url or f"{self.base_url}/api/reconciliation/sales",
            {"data_inicio_ccv": start, "data_fim_ccv": end},
            bearer_token,
        )

    async def dashboard_full(
        self, start: str, end: str, bearer_token: str
    ) -> Dict[str, Any]:
        _validate_period(start, end)
        return await self._get_url(
            f"{self.base_url}/api/dashboard/full",
            {"data_inicio_ccv": start, "data_fim_ccv": end},
            bearer_token,
        )

    async def _get_url(
        self, url: str, params: Dict[str, str], bearer_token: str
    ) -> Dict[str, Any]:
        if not bearer_token:
            raise DirectorMetricsError("Authenticated director access is required")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {bearer_token}"},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DirectorMetricsError("The management data source is unavailable") from exc
        if not isinstance(payload, dict):
            raise DirectorMetricsError("The management data source returned an invalid response")
        return payload


def sales_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    summary = payload.get("summary") or {}
    return {
        "period": payload.get("period"),
        "generated_at": payload.get("generated_at"),
        "official_source": payload.get("official_source"),
        "commercial_source": payload.get("commercial_source"),
        "sales": summary.get("official_sales", 0),
        "vgv": summary.get("official_vgv", "0"),
        "matched": summary.get("matched", 0),
        "pending_pipeimob_without_vista_gain": summary.get(
            "pipeimob_without_vista_gain", 0
        ),
        "value_mismatches": summary.get("value_mismatches", 0),
        "date_mismatches": summary.get("date_mismatches", 0),
        "source_note": (
            "Pipeimob confirms sales, official date and VGV; Vista supplies "
            "commercial ownership only for Status=Ganho. Fechamento alone is not a sale."
        ),
    }


def sales_divergences(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "status",
        "issues",
        "pipeimob_transaction_id",
        "pipeimob_contract_code",
        "vista_deal_id",
        "property_code",
        "official_sale_date",
        "vista_gain_date",
        "delay_days",
        "official_value",
        "vista_value",
        "value_difference",
        "commercial_broker",
        "fiscal_broker",
        "broker_roles_differ",
        "missing_fields",
    }
    rows = []
    for item in payload.get("items") or []:
        if item.get("status") == "CONCILIADO" and not item.get("issues"):
            continue
        rows.append({key: item.get(key) for key in allowed if key in item})
    return {
        "period": payload.get("period"),
        "generated_at": payload.get("generated_at"),
        "count": len(rows),
        "items": rows,
    }


def broker_sales(payload: Dict[str, Any]) -> Dict[str, Any]:
    grouped: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"sales": 0, "vgv": Decimal("0")}
    )
    missing = 0
    for item in payload.get("items") or []:
        if not item.get("official_sale_date") or not item.get("vista_deal_id"):
            continue
        broker = str(item.get("commercial_broker") or "").strip()
        if not broker:
            missing += 1
            continue
        grouped[broker]["sales"] += 1
        grouped[broker]["vgv"] += _decimal(item.get("official_value"))
    brokers = [
        {"commercial_broker": name, "sales": data["sales"], "vgv": str(data["vgv"])}
        for name, data in grouped.items()
    ]
    brokers.sort(key=lambda row: (-row["sales"], -_decimal(row["vgv"]), row["commercial_broker"]))
    return {
        "period": payload.get("period"),
        "generated_at": payload.get("generated_at"),
        "brokers": brokers,
        "missing_commercial_broker": missing,
        "attribution_rule": "Commercial broker from Vista; fiscal broker is not substituted.",
    }


def funnel_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "period": payload.get("period"),
        "generated_at": payload.get("generated_at"),
        "stages": _safe_stage_rows(payload.get("stages") or []),
        "rule": "Fechamento is a pipeline stage; only Vista Status=Ganho represents a sale.",
    }


def quality_summary(
    reconciliation: Dict[str, Any], dashboard: Dict[str, Any]
) -> Dict[str, Any]:
    summary = reconciliation.get("summary") or {}
    quality = dashboard.get("data_quality") or {}
    return {
        "period": reconciliation.get("period") or dashboard.get("period"),
        "generated_at": reconciliation.get("generated_at"),
        "reconciliation": {
            key: summary.get(key, 0)
            for key in (
                "official_sales",
                "matched",
                "pipeimob_without_vista_gain",
                "vista_without_pipeimob_contract",
                "value_mismatches",
                "date_mismatches",
                "no_automatic_link",
                "source_data_incomplete",
                "missing_commercial_broker",
            )
        },
        "pipeimob_data_quality": quality,
    }


def _validate_period(start: str, end: str, max_days: int = 366) -> None:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("Dates must use YYYY-MM-DD") from exc
    if start_date > end_date:
        raise ValueError("Start date cannot be after end date")
    if (end_date - start_date).days > max_days:
        raise ValueError(f"Date range cannot exceed {max_days} days")


def _safe_stage_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    allowed = {"name", "label", "stage", "etapa", "count", "volume", "value"}
    return [
        {key: row.get(key) for key in allowed if key in row}
        for row in rows
        if isinstance(row, dict)
    ]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")
