"""Read-only Vista CRM client for managerial funnel cohorts.

The client deliberately separates two concepts that are often mixed in BI:

* deals created in a period, grouped by their current stage; and
* stage-entry events that happened in a period.

``negocios/listar`` supports the first concept.  The second one requires a
documented and validated Vista history endpoint and is therefore never inferred
here.
"""

import concurrent.futures
import http.client
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.vista_sales_client import (
    VistaSalesAPIError,
    VistaSalesConfigurationError,
)


class VistaFunnelAPIError(VistaSalesAPIError):
    """Sanitized Vista funnel failure with an operational classification."""

    ALLOWED_CODES = {
        "vista_http_401",
        "vista_http_403",
        "vista_http_429",
        "vista_http_4xx",
        "vista_http_5xx",
        "vista_transport_error",
        "vista_invalid_json",
        "vista_invalid_contract",
    }

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = (
            error_code
            if error_code in self.ALLOWED_CODES
            else "vista_unavailable"
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
        request_attempts: int = 2,
        retry_backoff_seconds: float = 0.25,
        page_concurrency: int = 4,
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
        self.request_attempts = max(1, min(5, int(request_attempts)))
        self.retry_backoff_seconds = max(
            0.0, min(5.0, float(retry_backoff_seconds))
        )
        self.page_concurrency = max(1, min(8, int(page_concurrency)))
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
        try:
            request_attempts = int(
                os.getenv("VISTA_FUNNEL_REQUEST_MAX_ATTEMPTS", "2")
            )
        except ValueError:
            request_attempts = 2
        try:
            retry_backoff_seconds = float(
                os.getenv("VISTA_FUNNEL_RETRY_BACKOFF_SECONDS", "0.25")
            )
        except ValueError:
            retry_backoff_seconds = 0.25
        try:
            page_concurrency = int(
                os.getenv("VISTA_FUNNEL_PAGE_CONCURRENCY", "4")
            )
        except ValueError:
            page_concurrency = 4
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
            responsible_field=os.getenv(
                "VISTA_DEAL_RESPONSIBLE_FIELD", "Responsavel"
            ),
            request_attempts=request_attempts,
            retry_backoff_seconds=retry_backoff_seconds,
            page_concurrency=page_concurrency,
        )

    def fetch_created_deals(
        self, start_date: date, end_date: date
    ) -> List[Dict[str, Any]]:
        """Return unique deals created in the requested inclusive period."""
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        by_id: Dict[str, Dict[str, Any]] = {}

        def merge_payload(payload: Dict[str, Any]) -> int:
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
            return len(records)

        first_payload = self._fetch_page(start_date, end_date, 1)
        first_page_count = merge_payload(first_payload)
        pages = self._optional_int(first_payload.get("paginas"))

        if pages is not None:
            remaining_pages = list(range(2, max(1, pages) + 1))
            workers = min(self.page_concurrency, max(0, pages - 1))
            if workers:
                payloads_by_page: Dict[int, Dict[str, Any]] = {}
                failed_pages: List[int] = []
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers
                ) as executor:
                    futures = {
                        executor.submit(
                            self._fetch_page, start_date, end_date, page
                        ): page
                        for page in remaining_pages
                    }
                    for future in concurrent.futures.as_completed(futures):
                        page = futures[future]
                        try:
                            payloads_by_page[page] = future.result()
                        except VistaSalesAPIError:
                            failed_pages.append(page)

                for page in sorted(payloads_by_page):
                    merge_payload(payloads_by_page[page])

                # Vista occasionally throttles a request inside a concurrent
                # page batch. Recover only the failed pages serially so one
                # transient refusal does not invalidate the whole cohort.
                if failed_pages:
                    time.sleep(max(0.5, self.retry_backoff_seconds * 2))
                    for page in sorted(failed_pages):
                        merge_payload(
                            self._fetch_page(start_date, end_date, page)
                        )
        elif first_page_count >= 50:
            page = 2
            while True:
                payload = self._fetch_page(start_date, end_date, page)
                if merge_payload(payload) < 50:
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
        for attempt in range(1, self.request_attempts + 1):
            try:
                with self.opener(
                    request, timeout=self.timeout_seconds
                ) as response:
                    body = response.read()
                    payload = json.loads(body.decode("utf-8")) if body else {}
                break
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt >= self.request_attempts:
                    raise VistaFunnelAPIError(
                        f"Vista funnel page {page} failed with HTTP {exc.code}",
                        self._http_error_code(exc.code),
                    ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                socket.timeout,
                http.client.HTTPException,
                ssl.SSLError,
                OSError,
            ) as exc:
                if attempt >= self.request_attempts:
                    raise VistaFunnelAPIError(
                        f"Vista funnel page {page} transport failed",
                        "vista_transport_error",
                    ) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise VistaFunnelAPIError(
                    f"Vista funnel page {page} returned invalid JSON",
                    "vista_invalid_json",
                ) from exc

            if self.retry_backoff_seconds:
                time.sleep(self.retry_backoff_seconds * attempt)

        if not isinstance(payload, dict):
            raise VistaFunnelAPIError(
                f"Vista funnel page {page} returned an invalid contract",
                "vista_invalid_contract",
            )
        return payload

    @staticmethod
    def _http_error_code(status: int) -> str:
        if status == 401:
            return "vista_http_401"
        if status == 403:
            return "vista_http_403"
        if status == 429:
            return "vista_http_429"
        if 400 <= status <= 499:
            return "vista_http_4xx"
        if 500 <= status <= 599:
            return "vista_http_5xx"
        return "vista_unavailable"

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
    stage_status_counts: Dict[str, Dict[str, int]] = {}
    missing_stage = 0
    missing_status = 0
    missing_created_at = 0
    proposal_assignments: Dict[
        Tuple[str, str, str], Dict[str, Any]
    ] = {}

    for deal in deals:
        status = _normalize_label(deal.get("status"))
        stage = _normalize_label(deal.get("stage_name"))
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        else:
            missing_status += 1
        if stage:
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            if status:
                statuses = stage_status_counts.setdefault(stage, {})
                statuses[status] = statuses.get(status, 0) + 1
        else:
            missing_stage += 1
        if not deal.get("created_at"):
            missing_created_at += 1

        if stage and stage.casefold() == "proposta":
            team = _normalize_label(deal.get("team"))
            responsible = _normalize_label(deal.get("responsible"))
            created_date = str(deal.get("created_at") or "")[:10]
            assignment_key = (
                team.casefold() if team else "",
                responsible.casefold() if responsible else "",
                created_date,
            )
            assignment = proposal_assignments.setdefault(
                assignment_key,
                {
                    "team": team,
                    "responsible": responsible,
                    "created_date": created_date or None,
                    "current_stage_deals_count": 0,
                    "open_deals_count": 0,
                },
            )
            assignment["current_stage_deals_count"] += 1
            if status and status.casefold() in {"aberto", "em aberto", "open"}:
                assignment["open_deals_count"] += 1

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
    stage_status_breakdown = [
        {
            "stage": stage,
            "deals_count": sum(statuses.values()),
            "status_breakdown": rows(statuses, "status"),
        }
        for stage, statuses in sorted(
            stage_status_counts.items(),
            key=lambda item: (-sum(item[1].values()), item[0].casefold()),
        )
    ]
    proposal_status_counts = next(
        (
            statuses
            for stage, statuses in stage_status_counts.items()
            if stage.casefold() == "proposta"
        ),
        {},
    )
    proposal_open_count = sum(
        count
        for status, count in proposal_status_counts.items()
        if status.casefold() in {"aberto", "em aberto", "open"}
    )
    proposal_assignment_breakdown = sorted(
        proposal_assignments.values(),
        key=lambda row: (
            -int(row["open_deals_count"]),
            -int(row["current_stage_deals_count"]),
            str(row.get("team") or "").casefold(),
            str(row.get("responsible") or "").casefold(),
            str(row.get("created_date") or ""),
        ),
    )
    proposal_open_without_direct_team = sum(
        int(row["open_deals_count"])
        for row in proposal_assignment_breakdown
        if not row.get("team")
    )
    proposal_open_without_assignment_identity = sum(
        int(row["open_deals_count"])
        for row in proposal_assignment_breakdown
        if not row.get("team") and not row.get("responsible")
    )
    return {
        "created_deals": len(deals),
        "status_breakdown": rows(status_counts, "status"),
        "current_stage_breakdown": rows(stage_counts, "stage"),
        "stage_status_breakdown": stage_status_breakdown,
        "proposal": {
            "created_deals_currently_in_proposal": proposal_current_stage_count,
            "current_proposal_stage_status_breakdown": rows(
                proposal_status_counts, "status"
            ),
            "created_deals_in_proposal_stage_with_open_status": proposal_open_count,
            "assignment_breakdown": proposal_assignment_breakdown,
            "proposals_generated_in_period": None,
            "proposals_generated_status": "requires_stage_event_history",
        },
        "data_quality": {
            "missing_created_at": missing_created_at,
            "missing_current_stage": missing_stage,
            "missing_status": missing_status,
            "proposal_open_without_direct_team": proposal_open_without_direct_team,
            "proposal_open_without_assignment_identity": (
                proposal_open_without_assignment_identity
            ),
        },
    }
