"""Read-only Vista CRM client for managerial funnel cohorts.

The client deliberately separates two concepts that are often mixed in BI:

* deals created in a period, grouped by their current stage; and
* stage-entry events that happened in a period.

``negocios/listar`` supports the first concept.  The second one requires a
tenant-confirmed history/event contract and is therefore never inferred here.
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from services.vista_sales_client import (
    VistaSalesAPIError,
    VistaSalesConfigurationError,
)


def _normalize_label(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("Nome", "name", "Descricao", "description"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                value = candidate
                break
        else:
            return None
    if isinstance(value, (list, tuple, set, dict)):
        return None
    text = " ".join(str(value or "").split())
    return text or None


class VistaFunnelClient:
    """Fetch non-personal deal fields required for aggregate funnel metrics."""

    PAGINATION_KEYS = {"total", "paginas", "pagina", "quantidade"}
    BASE_FIELDS = [
        "Codigo",
        "CodigoPipe",
        "NomePipe",
        "Status",
        "EtapaAtual",
        "NomeEtapa",
        "UltimaAtualizacao",
        "ValorNegocio",
        "CorretorNegocio",
    ]
    FIELD_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

    def __init__(
        self,
        base_url: str,
        api_key: str,
        pipe_id: str,
        created_field: str = "DataInicial",
        timeout_seconds: int = 12,
        team_field: Optional[str] = None,
        agency_field: Optional[str] = None,
        capture_source_field: Optional[str] = None,
        responsible_field: Optional[str] = None,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not str(base_url or "").strip():
            raise VistaSalesConfigurationError("VISTA_API_BASE_URL is required")
        if not str(api_key or "").strip():
            raise VistaSalesConfigurationError("VISTA_API_KEY is required")
        if not str(pipe_id or "").strip():
            raise VistaSalesConfigurationError("VISTA_SALES_PIPE_ID is required")

        self.created_field = self._field_identifier(
            created_field, "VISTA_DEAL_CREATED_FIELD", required=True
        )
        self.team_field = self._field_identifier(
            team_field, "VISTA_DEAL_TEAM_FIELD"
        )
        self.agency_field = self._field_identifier(
            agency_field, "VISTA_DEAL_AGENCY_FIELD"
        )
        self.capture_source_field = self._field_identifier(
            capture_source_field, "VISTA_DEAL_CAPTURE_SOURCE_FIELD"
        )
        self.responsible_field = self._field_identifier(
            responsible_field, "VISTA_DEAL_RESPONSIBLE_FIELD"
        )

        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key)
        self.pipe_id = str(pipe_id).strip()
        self.timeout_seconds = max(1, min(30, int(timeout_seconds)))
        self.opener = opener or urllib.request.urlopen

        self.fields = list(self.BASE_FIELDS)
        for field in (
            self.created_field,
            self.team_field,
            self.agency_field,
            self.capture_source_field,
            self.responsible_field,
        ):
            if field and field not in self.fields:
                self.fields.append(field)

    @classmethod
    def from_env(cls) -> "VistaFunnelClient":
        return cls(
            base_url=os.getenv("VISTA_API_BASE_URL", ""),
            api_key=os.getenv("VISTA_API_KEY", ""),
            pipe_id=os.getenv("VISTA_SALES_PIPE_ID", ""),
            created_field=os.getenv("VISTA_DEAL_CREATED_FIELD", "DataInicial"),
            timeout_seconds=int(os.getenv("VISTA_HTTP_TIMEOUT_SECONDS", "12")),
            team_field=(
                os.getenv("VISTA_DEAL_TEAM_FIELD")
                or os.getenv("VISTA_SALES_TEAM_FIELD")
            ),
            agency_field=os.getenv("VISTA_DEAL_AGENCY_FIELD"),
            capture_source_field=os.getenv("VISTA_DEAL_CAPTURE_SOURCE_FIELD"),
            responsible_field=os.getenv("VISTA_DEAL_RESPONSIBLE_FIELD"),
        )

    def fetch_created_deals(
        self, start_date: date, end_date: date
    ) -> List[Dict[str, Any]]:
        """Return unique deals created in the requested inclusive period."""
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        page = 1
        by_id: Dict[str, Dict[str, Any]] = {}
        while True:
            payload = self._fetch_page(start_date, end_date, page)
            records = [
                value
                for key, value in payload.items()
                if key not in self.PAGINATION_KEYS and isinstance(value, dict)
            ]
            for record in records:
                normalized = self._normalize_deal(record)
                deal_id = str(normalized.get("deal_id") or "").strip()
                if deal_id:
                    by_id[deal_id] = normalized

            pages = self._optional_int(payload.get("paginas"))
            if (pages is not None and page >= pages) or (
                pages is None and len(records) < 50
            ):
                break
            page += 1

        return list(by_id.values())

    def _fetch_page(
        self, start_date: date, end_date: date, page: int
    ) -> Dict[str, Any]:
        pesquisa = {
            "fields": self.fields,
            "filter": {
                self.created_field: [start_date.isoformat(), end_date.isoformat()]
            },
            "order": {self.created_field: "asc"},
            "paginacao": {"pagina": page, "quantidade": 50},
        }
        query = urllib.parse.urlencode(
            {
                "key": self.api_key,
                "codigo_pipe": self.pipe_id,
                "showtotal": "1",
                "pesquisa": json.dumps(
                    pesquisa, ensure_ascii=False, separators=(",", ":")
                ),
            }
        )
        request = urllib.request.Request(
            f"{self.base_url}/negocios/listar?{query}",
            headers={"Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                payload = json.loads(body.decode("utf-8")) if body else {}
        except urllib.error.HTTPError as exc:
            raise VistaSalesAPIError(
                f"Vista funnel request failed with HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise VistaSalesAPIError("Vista funnel request is unavailable") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VistaSalesAPIError("Vista funnel returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise VistaSalesAPIError("Vista funnel response must be a JSON object")
        return payload

    def _normalize_deal(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Whitelist operational fields and discard client data if returned."""
        return {
            "deal_id": record.get("Codigo"),
            "pipe_id": record.get("CodigoPipe"),
            "pipe_name": _normalize_label(record.get("NomePipe")),
            "created_at": record.get(self.created_field),
            "created_field": self.created_field,
            "status": _normalize_label(record.get("Status")),
            "stage_id": record.get("EtapaAtual"),
            "stage_name": _normalize_label(record.get("NomeEtapa")),
            "last_update": record.get("UltimaAtualizacao"),
            "potential_value": record.get("ValorNegocio"),
            "commercial_broker_id": record.get("CorretorNegocio"),
            "team": _normalize_label(
                record.get(self.team_field) if self.team_field else None
            ),
            "agency": _normalize_label(
                record.get(self.agency_field) if self.agency_field else None
            ),
            "capture_source": _normalize_label(
                record.get(self.capture_source_field)
                if self.capture_source_field
                else None
            ),
            "responsible": _normalize_label(
                record.get(self.responsible_field)
                if self.responsible_field
                else None
            ),
        }

    @classmethod
    def _field_identifier(
        cls, value: Optional[str], env_name: str, required: bool = False
    ) -> Optional[str]:
        text = str(value or "").strip()
        if not text and not required:
            return None
        if not text:
            raise VistaSalesConfigurationError(f"{env_name} is required")
        if not cls.FIELD_IDENTIFIER.fullmatch(text):
            raise VistaSalesConfigurationError(
                f"{env_name} must be a Vista field identifier"
            )
        return text

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None


def summarize_created_deal_cohort(
    deals: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build aggregate-only metrics from the Vista created-deal cohort."""
    status_counts: Dict[str, int] = {}
    stage_counts: Dict[str, int] = {}
    missing_stage = 0
    missing_status = 0
    missing_created_at = 0

    for deal in deals:
        status = _normalize_label(deal.get("status"))
        stage = _normalize_label(deal.get("stage_name"))
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        else:
            missing_status += 1
        if stage:
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
        else:
            missing_stage += 1
        if not deal.get("created_at"):
            missing_created_at += 1

    def rows(values: Dict[str, int], label_key: str) -> List[Dict[str, Any]]:
        return [
            {label_key: label, "deals_count": count}
            for label, count in sorted(
                values.items(), key=lambda item: (-item[1], item[0].casefold())
            )
        ]

    proposal_current_stage_count = sum(
        count
        for stage, count in stage_counts.items()
        if stage.casefold() == "proposta"
    )
    return {
        "created_deals": len(deals),
        "status_breakdown": rows(status_counts, "status"),
        "current_stage_breakdown": rows(stage_counts, "stage"),
        "proposal": {
            "created_deals_currently_in_proposal": proposal_current_stage_count,
            "proposals_generated_in_period": None,
            "proposals_generated_status": "requires_stage_event_history",
        },
        "data_quality": {
            "missing_created_at": missing_created_at,
            "missing_current_stage": missing_stage,
            "missing_status": missing_status,
        },
    }
