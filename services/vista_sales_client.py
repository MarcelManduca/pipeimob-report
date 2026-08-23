import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sanitization helper to redact any keys or sensitive tokens before logging
SENSITIVE_PATTERNS = [
    re.compile(r"(key|api_key|token|authorization|password|secret)=([^&]+)", re.IGNORECASE),
    re.compile(r"([\x27\x22](?:key|api_key|token|authorization|password|secret)[\x27\x22]\s*:\s*[\x27\x22])([^\x27\x22]+)([\x27\x22])", re.IGNORECASE),
]

def sanitize_vista_log_body(text: str, max_length: int = 500) -> str:
    if not text:
        return ""
    sanitized = str(text)
    for pattern in SENSITIVE_PATTERNS:
        sanitized = pattern.sub(r"\1=[REDACTED]", sanitized)
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "... [TRUNCATED]"
    return sanitized


class VistaSalesConfigurationError(RuntimeError):
    """Raised when the tenant connection is not configured."""


class VistaSalesAPIError(RuntimeError):
    """Raised when the CRM Vista API fails, times out or is unreachable."""


class VistaSalesClient:
    """Official read-only CRM Vista client for sales reconciliation.

    Queries exclusively closed sales ("Ganho") and their responsible brokers.
    Never requests client names, CPF/CNPJ, phones, emails or notes.
    """

    FIELDS = [
        "Codigo",
        "CodigoImovel",
        "Status",
        "DataFinal",
        "UltimaAtualizacao",
        "ValorNegocio",
        "CodigoPipe",
        "NomePipe",
        "EtapaAtual",
        "NomeEtapa",
        "CorretorNegocio",
    ]
    PAGINATION_KEYS = {"total", "paginas", "pagina", "quantidade"}

    def __init__(
        self,
        base_url: str,
        api_key: str,
        pipe_id: str,
        timeout_seconds: int = 12,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not str(base_url or "").strip():
            raise VistaSalesConfigurationError("VISTA_API_BASE_URL is required")
        if not str(api_key or "").strip():
            raise VistaSalesConfigurationError("VISTA_API_KEY is required")
        if not str(pipe_id or "").strip():
            raise VistaSalesConfigurationError("VISTA_SALES_PIPE_ID is required")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.pipe_id = str(pipe_id).strip()
        self.timeout_seconds = max(1, min(30, int(timeout_seconds)))
        self.opener = opener or urllib.request.urlopen

    @classmethod
    def from_env(cls) -> "VistaSalesClient":
        return cls(
            base_url=os.getenv("VISTA_API_BASE_URL", ""),
            api_key=os.getenv("VISTA_API_KEY", ""),
            pipe_id=os.getenv("VISTA_SALES_PIPE_ID", ""),
            timeout_seconds=int(os.getenv("VISTA_HTTP_TIMEOUT_SECONDS", "12")),
        )

    def fetch_gains(self, start_date: date, end_date: date) -> List[Dict[str, Any]]:
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        page = 1
        gains: List[Dict[str, Any]] = []
        while True:
            payload = self._fetch_page(start_date, end_date, page)
            records = [
                value
                for key, value in payload.items()
                if key not in self.PAGINATION_KEYS and isinstance(value, dict)
            ]
            normalized = [self._normalize_gain(record) for record in records]
            gains.extend(gain for gain in normalized if self._is_gain(gain["status"]))

            pages = self._optional_int(payload.get("paginas"))
            if pages is not None and page >= pages:
                break
            if pages is None and len(records) < 50:
                break
            page += 1

        broker_names = self._fetch_user_names(
            {
                str(gain["commercial_broker_id"]).strip()
                for gain in gains
                if gain.get("commercial_broker_id") not in (None, "")
            }
        )
        for gain in gains:
            broker_id = str(gain.get("commercial_broker_id") or "").strip()
            gain["commercial_broker_name"] = broker_names.get(broker_id)

        return gains

    def _fetch_user_names(self, broker_ids: set[str]) -> Dict[str, str]:
        """Resolve Vista broker IDs in one filtered, non-personal users query."""
        if not broker_ids:
            return {}

        page = 1
        names: Dict[str, str] = {}
        while True:
            pesquisa = {
                "fields": ["Codigo", "Nome"],
                "filter": {"Codigo": sorted(broker_ids)},
                "paginacao": {"pagina": page, "quantidade": 50},
            }
            query = urllib.parse.urlencode(
                {
                    "key": self.api_key,
                    "showtotal": "1",
                    "pesquisa": json.dumps(
                        pesquisa, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            )
            endpoint_path = "/usuarios/listar"
            request = urllib.request.Request(
                f"{self.base_url}{endpoint_path}?{query}",
                headers={"Accept": "application/json"},
            )
            start_time = time.perf_counter()
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    payload = json.loads(body.decode("utf-8")) if body else {}
            except Exception as exc:
                duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
                error_body = ""
                if isinstance(exc, urllib.error.HTTPError):
                    try:
                        error_body = exc.read().decode("utf-8", errors="replace")
                    except Exception:
                        pass
                logger.warning(
                    json.dumps({
                        "event": "vista_sales_users_lookup_failed",
                        "exception_class": type(exc).__name__,
                        "operation": "list_users",
                        "endpoint_path": endpoint_path,
                        "duration_ms": duration_ms,
                        "sanitized_body": sanitize_vista_log_body(error_body or str(exc)),
                    })
                )
                # A broker-name lookup must not make official sales unavailable.
                return names

            if not isinstance(payload, dict):
                return names
            records = [
                value
                for key, value in payload.items()
                if key not in self.PAGINATION_KEYS and isinstance(value, dict)
            ]
            for record in records:
                code = str(record.get("Codigo") or "").strip()
                name = str(record.get("Nome") or "").strip()
                if code and name:
                    names[code] = name

            pages = self._optional_int(payload.get("paginas"))
            if (pages is not None and page >= pages) or (
                pages is None and len(records) < 50
            ):
                break
            page += 1

        return names

    def _fetch_page(
        self, start_date: date, end_date: date, page: int
    ) -> Dict[str, Any]:
        pesquisa = {
            "fields": self.FIELDS,
            "filter": {
                "Status": "Ganho",
                "DataFinal": [start_date.isoformat(), end_date.isoformat()],
            },
            "order": {"DataFinal": "asc"},
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
        endpoint_path = "/negocios/listar"
        request = urllib.request.Request(
            f"{self.base_url}{endpoint_path}?{query}",
            headers={"Accept": "application/json"},
        )

        start_time = time.perf_counter()
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                if not body:
                    return {}
                payload = json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            content_type = exc.headers.get("content-type", "") if exc.headers else ""
            logger.error(
                json.dumps({
                    "event": "vista_sales_api_failed",
                    "exception_class": type(exc).__name__,
                    "operation": "list_gains",
                    "endpoint_path": endpoint_path,
                    "http_status": exc.code,
                    "duration_ms": duration_ms,
                    "content_type": content_type,
                    "sanitized_body": sanitize_vista_log_body(error_body),
                    "error_kind": "http_error",
                })
            )
            raise VistaSalesAPIError(
                f"Vista request failed with HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            is_timeout = isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            logger.error(
                json.dumps({
                    "event": "vista_sales_api_failed",
                    "exception_class": type(exc).__name__,
                    "operation": "list_gains",
                    "endpoint_path": endpoint_path,
                    "http_status": None,
                    "duration_ms": duration_ms,
                    "content_type": None,
                    "sanitized_body": sanitize_vista_log_body(str(exc)),
                    "error_kind": "timeout" if is_timeout else "connection_error",
                })
            )
            raise VistaSalesAPIError("Vista request is unavailable") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            raw_sample = ""
            try:
                raw_sample = body.decode("utf-8", errors="replace") if "body" in locals() and body else ""
            except Exception:
                pass
            logger.error(
                json.dumps({
                    "event": "vista_sales_api_failed",
                    "exception_class": type(exc).__name__,
                    "operation": "list_gains",
                    "endpoint_path": endpoint_path,
                    "http_status": 200,
                    "duration_ms": duration_ms,
                    "content_type": "invalid_format",
                    "sanitized_body": sanitize_vista_log_body(raw_sample or str(exc)),
                    "error_kind": "invalid_json",
                })
            )
            raise VistaSalesAPIError("Vista returned invalid JSON") from exc

        if not isinstance(payload, dict):
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            logger.error(
                json.dumps({
                    "event": "vista_sales_api_failed",
                    "exception_class": "InvalidPayloadType",
                    "operation": "list_gains",
                    "endpoint_path": endpoint_path,
                    "http_status": 200,
                    "duration_ms": duration_ms,
                    "content_type": "application/json",
                    "sanitized_body": sanitize_vista_log_body(str(payload)),
                    "error_kind": "invalid_json_structure",
                })
            )
            raise VistaSalesAPIError("Vista response must be a JSON object")
        return payload

    @staticmethod
    def _normalize_gain(record: Dict[str, Any]) -> Dict[str, Any]:
        """Exclude client fields even if a tenant unexpectedly returns them."""
        return {
            "deal_id": record.get("Codigo"),
            "property_code": record.get("CodigoImovel"),
            "status": record.get("Status"),
            "gain_date": record.get("DataFinal"),
            "last_update": record.get("UltimaAtualizacao"),
            "deal_value": record.get("ValorNegocio"),
            "pipe_id": record.get("CodigoPipe"),
            "pipe_name": record.get("NomePipe"),
            "stage_id": record.get("EtapaAtual"),
            "stage_name": record.get("NomeEtapa"),
            "commercial_broker_id": VistaSalesClient._first_present(
                record, "CorretorNegocio"
            ),
            "commercial_broker_name": None,
        }

    @staticmethod
    def _first_present(record: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = record.get(key)
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _is_gain(value: Any) -> bool:
        """Never let the pipeline stage \"Fechamento\" imply a completed sale."""
        return str(value or "").strip().casefold() == "ganho"

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
