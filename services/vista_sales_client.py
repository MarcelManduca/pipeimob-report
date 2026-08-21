"""Minimal, privacy-preserving client for won deals in Vista CRM."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Dict, List, Optional


class VistaSalesConfigurationError(RuntimeError):
    """Raised when the tenant connection is not configured."""


class VistaSalesAPIError(RuntimeError):
    """Raised without including the Vista API key or response personal data."""


class VistaSalesClient:
    """Read only the fields required to reconcile sales."""

    FIELDS = [
        "Codigo",
        "NomePipe",
        "UltimaAtualizacao",
        "Status",
        "DataFinal",
        "ValorNegocio",
        "CodigoPipe",
        "EtapaAtual",
        "NomeEtapa",
        "CodigoImovel",
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

        return gains

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
        request = urllib.request.Request(
            f"{self.base_url}/negocios/listar?{query}",
            headers={"Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                if not body:
                    return {}
                payload = json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise VistaSalesAPIError(
                f"Vista request failed with HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise VistaSalesAPIError("Vista request is unavailable") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VistaSalesAPIError("Vista returned invalid JSON") from exc

        if not isinstance(payload, dict):
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
                record, "CodigoCorretor", "CorretorNegocio"
            ),
            "commercial_broker_name": VistaSalesClient._first_present(
                record, "Corretor", "NomeCorretor"
            ),
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
        """Never let the pipeline stage "Fechamento" imply a completed sale."""
        return str(value or "").strip().casefold() == "ganho"

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
