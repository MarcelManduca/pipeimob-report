"""Privacy-preserving client for Vista CRM organizational hierarchy and stable ID coverage.

Reuses existing Vista CRM client conventions and models:
- Bounded queries (requires start_date, end_date, pipe_id, and max_pages).
- Strictly uses proven canonical and explicitly configured fields (no speculative probing).
- Separates IDs from text labels: plain strings from label fields are NEVER promoted to IDs.
- Nested IDs in objects (e.g. {"Codigo": ...}) are supported.
- Reports completeness metadata (pages_reported, pages_fetched, records_fetched, truncated, complete).
- Categorizes unknown roles as 'unknown', never defaulting to 'broker'.
- Halts probing on 2 equivalent consecutive failures (circuit breaker).
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from services.vista_sales_client import (
    VistaSalesAPIError,
    VistaSalesConfigurationError,
)


class VistaOrganizationAPIError(VistaSalesAPIError):
    """Sanitized diagnostic failure for organizational queries."""

    ALLOWED_CODES = {
        "vista_transport_error",
        "vista_invalid_json",
        "vista_invalid_contract",
        "vista_circuit_broken",
        "vista_auth_failure",
        "vista_field_negotiation_failed",
        "vista_deal_query_failed",
        "vista_not_configured",
    }

    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        safe_http_code = re.fullmatch(r"vista_http_[45]\d{2}", error_code or "")
        self.error_code = (
            error_code
            if (error_code in self.ALLOWED_CODES or safe_http_code)
            else "vista_unavailable"
        )


class VistaOrganizationClient:
    """Query Vista organizational structure with bounded pagination and strict ID/label separation."""

    PAGINATION_KEYS = {"total", "paginas", "pagina", "quantidade"}
    FIELD_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

    # Proven canonical fields for /usuarios/listar
    USER_CORE_FIELDS = ["Codigo"]
    USER_DISCOVERY_FIELDS = [
        "Status",
        "Ativo",
        "Cargo",
        "Funcao",
        "Perfil",
    ]
    USER_ORGANIZATIONAL_FIELD_TOKENS = (
        "equipe",
        "time",
        "team",
        "gerente",
        "gestor",
        "manager",
        "agencia",
        "loja",
        "filial",
        "cargo",
        "funcao",
        "perfil",
        "status",
        "situacao",
        "ativo",
    )

    # Proven canonical fields for /negocios/listar
    DEAL_CORE_FIELDS = [
        "Codigo",
        "CodigoPipe",
        "CorretorNegocio",
        "Status",
        "EtapaAtual",
    ]

    def __init__(
        self,
        base_url: str,
        api_key: str,
        pipe_id: Optional[str] = None,
        created_field: str = "DataInicial",
        user_team_id_field: Optional[str] = None,
        user_team_field: Optional[str] = None,
        user_manager_id_field: Optional[str] = None,
        user_manager_field: Optional[str] = None,
        user_agency_id_field: Optional[str] = None,
        user_agency_field: Optional[str] = None,
        deal_team_id_field: Optional[str] = None,
        deal_team_field: Optional[str] = None,
        deal_manager_id_field: Optional[str] = None,
        deal_manager_field: Optional[str] = None,
        deal_agency_id_field: Optional[str] = None,
        deal_agency_field: Optional[str] = None,
        timeout_seconds: int = 12,
        max_failure_threshold: int = 2,
        max_pages_limit: int = 10,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        if not str(base_url or "").strip():
            raise VistaSalesConfigurationError("VISTA_API_BASE_URL is required")
        if not str(api_key or "").strip():
            raise VistaSalesConfigurationError("VISTA_API_KEY is required")

        self.base_url = str(base_url).rstrip("/")
        self.api_key = str(api_key)
        self.pipe_id = str(pipe_id).strip() if pipe_id else None
        self.created_field = self._field_identifier(
            created_field, "VISTA_DEAL_CREATED_FIELD", required=True
        )

        # User configured fields
        self.user_team_id_field = self._field_identifier(
            user_team_id_field, "VISTA_USER_TEAM_ID_FIELD"
        )
        self.user_team_field = self._field_identifier(
            user_team_field, "VISTA_USER_TEAM_FIELD"
        )
        self.user_manager_id_field = self._field_identifier(
            user_manager_id_field, "VISTA_USER_MANAGER_ID_FIELD"
        )
        self.user_manager_field = self._field_identifier(
            user_manager_field, "VISTA_USER_MANAGER_FIELD"
        )
        self.user_agency_id_field = self._field_identifier(
            user_agency_id_field, "VISTA_USER_AGENCY_ID_FIELD"
        )
        self.user_agency_field = self._field_identifier(
            user_agency_field, "VISTA_USER_AGENCY_FIELD"
        )

        # Deal configured fields
        self.deal_team_id_field = self._field_identifier(
            deal_team_id_field, "VISTA_DEAL_TEAM_ID_FIELD"
        )
        self.deal_team_field = self._field_identifier(
            deal_team_field, "VISTA_DEAL_TEAM_FIELD"
        )
        self.deal_manager_id_field = self._field_identifier(
            deal_manager_id_field, "VISTA_DEAL_MANAGER_ID_FIELD"
        )
        self.deal_manager_field = self._field_identifier(
            deal_manager_field, "VISTA_DEAL_MANAGER_FIELD"
        )
        self.deal_agency_id_field = self._field_identifier(
            deal_agency_id_field, "VISTA_DEAL_AGENCY_ID_FIELD"
        )
        self.deal_agency_field = self._field_identifier(
            deal_agency_field, "VISTA_DEAL_AGENCY_FIELD"
        )

        self.timeout_seconds = max(1, min(30, int(timeout_seconds)))
        self.max_failure_threshold = max(1, min(5, int(max_failure_threshold)))
        self.max_pages_limit = max(1, min(20, int(max_pages_limit)))
        self.opener = opener or urllib.request.urlopen

        # Circuit breaker state
        self._consecutive_failures = 0
        self._last_error_category: Optional[str] = None
        self._circuit_broken = False
        self._probed_fields: Dict[str, Set[str]] = {
            "accepted": set(),
            "rejected": set(),
        }

    @classmethod
    def from_env(cls) -> "VistaOrganizationClient":
        return cls(
            base_url=os.getenv("VISTA_API_BASE_URL", ""),
            api_key=os.getenv("VISTA_API_KEY", ""),
            pipe_id=os.getenv("VISTA_SALES_PIPE_ID"),
            created_field=os.getenv("VISTA_DEAL_CREATED_FIELD", "DataInicial"),
            user_team_id_field=os.getenv("VISTA_USER_TEAM_ID_FIELD"),
            user_team_field=os.getenv("VISTA_USER_TEAM_FIELD"),
            user_manager_id_field=os.getenv("VISTA_USER_MANAGER_ID_FIELD"),
            user_manager_field=os.getenv("VISTA_USER_MANAGER_FIELD"),
            user_agency_id_field=os.getenv("VISTA_USER_AGENCY_ID_FIELD"),
            user_agency_field=os.getenv("VISTA_USER_AGENCY_FIELD"),
            deal_team_id_field=os.getenv("VISTA_DEAL_TEAM_ID_FIELD"),
            deal_team_field=(
                os.getenv("VISTA_DEAL_TEAM_FIELD")
                or os.getenv("VISTA_SALES_TEAM_FIELD")
                or "EquipeNegocio"
            ),
            deal_manager_id_field=os.getenv("VISTA_DEAL_MANAGER_ID_FIELD"),
            deal_manager_field=os.getenv("VISTA_DEAL_MANAGER_FIELD"),
            deal_agency_id_field=os.getenv("VISTA_DEAL_AGENCY_ID_FIELD"),
            deal_agency_field=os.getenv("VISTA_DEAL_AGENCY_FIELD"),
            timeout_seconds=int(os.getenv("VISTA_HTTP_TIMEOUT_SECONDS", "12")),
            max_pages_limit=int(os.getenv("VISTA_ORGANIZATION_MAX_PAGES", "10")),
        )

    def is_circuit_broken(self) -> bool:
        return self._circuit_broken

    def get_probed_fields(self) -> Dict[str, List[str]]:
        return {
            "accepted": sorted(self._probed_fields["accepted"]),
            "rejected": sorted(self._probed_fields["rejected"]),
        }

    def fetch_anonymized_users(
        self, max_pages: Optional[int] = None
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Fetch user records with strict PII scrubbing and return completeness metadata."""
        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )

        active_fields = self._negotiate_user_fields()
        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )
        if not active_fields:
            return [], {
                "pages_reported": 0,
                "pages_fetched": 0,
                "records_fetched": 0,
                "truncated": False,
                "complete": True,
            }

        page_limit = min(
            self.max_pages_limit,
            max(1, int(max_pages if max_pages is not None else self.max_pages_limit)),
        )
        users: List[Dict[str, Any]] = []
        page = 1
        pages_reported = None

        while page <= page_limit:
            payload = self._execute_user_query(active_fields, page=page)
            if not payload:
                break

            if pages_reported is None:
                pages_reported = self._optional_int(payload.get("paginas"))

            records = [
                value
                for key, value in payload.items()
                if key not in self.PAGINATION_KEYS and isinstance(value, dict)
            ]

            for record in records:
                sanitized = self._sanitize_user_record(record)
                if sanitized.get("user_id"):
                    users.append(sanitized)

            if pages_reported is not None and page >= pages_reported:
                break
            if pages_reported is None and len(records) < 50:
                break
            page += 1

        # /corretores/listar is the authoritative broker directory for this
        # tenant. It exposes only stable code and name; we retain the code only
        # and use it to classify matching users without persisting PII.
        broker_ids = self._fetch_broker_ids(max_pages=page_limit)
        for user in users:
            if user.get("user_id") in broker_ids:
                user["role_type"] = "broker"

        pages_fetched = min(page, page_limit) if users else 0
        total_pages = pages_reported if pages_reported is not None else pages_fetched
        truncated = bool(total_pages > pages_fetched)

        completeness = {
            "pages_reported": total_pages,
            "pages_fetched": pages_fetched,
            "records_fetched": len(users),
            "truncated": truncated,
            "complete": not truncated,
        }

        return users, completeness

    def _fetch_broker_ids(self, max_pages: int) -> Set[str]:
        broker_ids: Set[str] = set()
        page = 1
        while page <= max_pages:
            payload = self._execute_broker_query(page=page)
            records = [
                value
                for key, value in payload.items()
                if key not in self.PAGINATION_KEYS and isinstance(value, dict)
            ]
            for record in records:
                broker_id = self._extract_stable_id(
                    record, "Codigo", "CodigoCorretor", "id"
                )
                if broker_id:
                    broker_ids.add(broker_id)

            pages_reported = self._optional_int(payload.get("paginas"))
            if pages_reported is not None and page >= pages_reported:
                break
            if pages_reported is None and len(records) < 50:
                break
            page += 1
        return broker_ids

    def fetch_bounded_deal_associations(
        self,
        start_date: date,
        end_date: date,
        pipe_id: Optional[str] = None,
        max_pages: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Fetch deal associations bounded strictly by period, pipe, and page limit."""
        if start_date > end_date:
            raise ValueError("start_date cannot be after end_date")

        effective_pipe_id = (
            str(pipe_id).strip()
            if pipe_id
            else (str(self.pipe_id).strip() if self.pipe_id else None)
        )
        if not effective_pipe_id:
            raise VistaSalesConfigurationError(
                "VISTA_SALES_PIPE_ID is required for bounded deal queries"
            )

        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )

        active_fields = self._negotiate_deal_fields(effective_pipe_id)
        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )
        if not active_fields:
            return [], {
                "pages_reported": 0,
                "pages_fetched": 0,
                "records_fetched": 0,
                "truncated": False,
                "complete": True,
            }

        page_limit = min(
            self.max_pages_limit,
            max(1, int(max_pages if max_pages is not None else self.max_pages_limit)),
        )
        deals: List[Dict[str, Any]] = []
        page = 1
        pages_reported = None

        while page <= page_limit:
            payload = self._execute_deal_query(
                active_fields,
                start_date=start_date,
                end_date=end_date,
                pipe_id=effective_pipe_id,
                page=page,
            )
            if not payload:
                break

            if pages_reported is None:
                pages_reported = self._optional_int(payload.get("paginas"))

            records = [
                value
                for key, value in payload.items()
                if key not in self.PAGINATION_KEYS and isinstance(value, dict)
            ]

            for record in records:
                sanitized = self._sanitize_deal_record(record)
                if sanitized.get("deal_id"):
                    deals.append(sanitized)

            if pages_reported is not None and page >= pages_reported:
                break
            if pages_reported is None and len(records) < 50:
                break
            page += 1

        pages_fetched = min(page, page_limit) if deals else 0
        total_pages = pages_reported if pages_reported is not None else pages_fetched
        truncated = bool(total_pages > pages_fetched)

        completeness = {
            "pages_reported": total_pages,
            "pages_fetched": pages_fetched,
            "records_fetched": len(deals),
            "truncated": truncated,
            "complete": not truncated,
        }

        return deals, completeness

    def _negotiate_user_fields(self) -> List[str]:
        # Vista documents /usuarios/listarcampos as the authoritative catalog
        # for tenant-specific user fields. Prefer it over guessing field names,
        # while retaining bounded probes as a compatibility fallback.
        catalog_fields = self._fetch_user_field_catalog()
        configured_optionals = [
            f
            for f in (
                self.user_team_id_field,
                self.user_team_field,
                self.user_manager_id_field,
                self.user_manager_field,
                self.user_agency_id_field,
                self.user_agency_field,
            )
            if f
        ]
        catalog_organization_fields = [
            field
            for field in catalog_fields
            if any(
                token in field.casefold()
                for token in self.USER_ORGANIZATIONAL_FIELD_TOKENS
            )
        ][:40]
        candidates = self.USER_CORE_FIELDS + catalog_organization_fields + configured_optionals
        if not catalog_fields:
            candidates += self.USER_DISCOVERY_FIELDS
        unique_candidates: List[str] = []
        for c in candidates:
            if c not in unique_candidates:
                unique_candidates.append(c)

        try:
            self._execute_user_query(self.USER_CORE_FIELDS, page=1, is_probe=True)
            accepted = list(self.USER_CORE_FIELDS)
            self._probed_fields["accepted"].update(self.USER_CORE_FIELDS)
        except VistaOrganizationAPIError as exc:
            raise

        optional_candidates = [
            f for f in unique_candidates if f not in self.USER_CORE_FIELDS
        ]
        for field in optional_candidates:
            if self._circuit_broken:
                break
            try:
                self._execute_user_query(accepted + [field], page=1, is_probe=True)
                accepted.append(field)
                self._probed_fields["accepted"].add(field)
            except VistaOrganizationAPIError as probe_exc:
                self._probed_fields["rejected"].add(field)
                if probe_exc.error_code not in (
                    "vista_http_400",
                    "vista_invalid_contract",
                ):
                    raise

        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )

        return accepted

    def _fetch_user_field_catalog(self) -> List[str]:
        """Return safe technical identifiers advertised by usuarios/listarcampos.

        The endpoint has had more than one response shape across Vista tenants,
        so identifiers are collected recursively from object keys and scalar
        values. Only strict field identifiers are retained; labels containing
        spaces, payload data and arbitrary text are ignored.
        """
        query = urllib.parse.urlencode({"key": self.api_key})
        url = f"{self.base_url}/usuarios/listarcampos?{query}"
        try:
            payload = self._send_request(
                url, endpoint_tag="usuarios_listarcampos", is_probe=True
            )
        except VistaOrganizationAPIError as exc:
            if exc.error_code in ("vista_http_400", "vista_http_404"):
                return []
            raise

        found: Set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if isinstance(key, str) and self.FIELD_IDENTIFIER.fullmatch(key):
                        found.add(key)
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)
            elif isinstance(value, str) and self.FIELD_IDENTIFIER.fullmatch(value):
                found.add(value)

        visit(payload)
        return sorted(found)

    def _negotiate_deal_fields(self, pipe_id: str) -> List[str]:
        optional_configured = [
            f
            for f in (
                self.created_field,
                self.deal_team_id_field,
                self.deal_team_field,
                self.deal_agency_id_field,
                self.deal_agency_field,
                self.deal_manager_id_field,
                self.deal_manager_field,
            )
            if f
        ]
        candidates = self.DEAL_CORE_FIELDS + optional_configured
        unique_candidates: List[str] = []
        for c in candidates:
            if c not in unique_candidates:
                unique_candidates.append(c)

        probe_date = date(2026, 1, 1)
        try:
            self._execute_deal_query(
                unique_candidates,
                start_date=probe_date,
                end_date=probe_date,
                pipe_id=pipe_id,
                page=1,
                is_probe=True,
            )
            self._probed_fields["accepted"].update(unique_candidates)
            return unique_candidates
        except VistaOrganizationAPIError as exc:
            if self._circuit_broken:
                raise
            if exc.error_code != "vista_http_400":
                raise

        try:
            self._execute_deal_query(
                self.DEAL_CORE_FIELDS,
                start_date=probe_date,
                end_date=probe_date,
                pipe_id=pipe_id,
                page=1,
                is_probe=True,
            )
            accepted = list(self.DEAL_CORE_FIELDS)
            self._probed_fields["accepted"].update(self.DEAL_CORE_FIELDS)
        except VistaOrganizationAPIError as exc:
            if self._circuit_broken:
                raise
            return []

        optional_candidates = [
            f for f in unique_candidates if f not in self.DEAL_CORE_FIELDS
        ]
        for field in optional_candidates:
            if self._circuit_broken:
                break
            try:
                self._execute_deal_query(
                    accepted + [field],
                    start_date=probe_date,
                    end_date=probe_date,
                    pipe_id=pipe_id,
                    page=1,
                    is_probe=True,
                )
                accepted.append(field)
                self._probed_fields["accepted"].add(field)
            except VistaOrganizationAPIError as probe_exc:
                self._probed_fields["rejected"].add(field)
                if self._circuit_broken or probe_exc.error_code not in (
                    "vista_http_400",
                    "vista_invalid_contract",
                ):
                    break

        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )

        return accepted

    def _execute_user_query(
        self, fields: List[str], page: int = 1, is_probe: bool = False
    ) -> Dict[str, Any]:
        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )

        pesquisa = {
            "fields": fields,
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
        url = f"{self.base_url}/usuarios/listar?{query}"
        return self._send_request(
            url, endpoint_tag="usuarios_listar", is_probe=is_probe
        )

    def _execute_broker_query(self, page: int = 1) -> Dict[str, Any]:
        pesquisa = {
            "fields": ["Codigo"],
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
        url = f"{self.base_url}/corretores/listar?{query}"
        return self._send_request(url, endpoint_tag="corretores_listar")

    def _execute_deal_query(
        self,
        fields: List[str],
        start_date: date,
        end_date: date,
        pipe_id: str,
        page: int = 1,
        is_probe: bool = False,
    ) -> Dict[str, Any]:
        if self._circuit_broken:
            raise VistaOrganizationAPIError(
                "Circuit breaker is open after repeated failures",
                "vista_circuit_broken",
            )

        pesquisa = {
            "fields": fields,
            "filter": {
                self.created_field: [
                    start_date.isoformat(),
                    end_date.isoformat(),
                ]
            },
            "paginacao": {"pagina": page, "quantidade": 50},
        }
        query_params: Dict[str, Any] = {
            "key": self.api_key,
            "codigo_pipe": pipe_id,
            "showtotal": "1",
            "pesquisa": json.dumps(
                pesquisa, ensure_ascii=False, separators=(",", ":")
            ),
        }
        url = f"{self.base_url}/negocios/listar?{urllib.parse.urlencode(query_params)}"
        return self._send_request(
            url, endpoint_tag="negocios_listar", is_probe=is_probe
        )

    def _send_request(
        self, url: str, endpoint_tag: str, is_probe: bool = False
    ) -> Dict[str, Any]:
        request = urllib.request.Request(
            url, headers={"Accept": "application/json"}
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                payload = json.loads(body.decode("utf-8")) if body else {}

            self._consecutive_failures = 0
            self._last_error_category = None

            if not isinstance(payload, dict):
                self._record_failure("invalid_contract")
                raise VistaOrganizationAPIError(
                    f"{endpoint_tag} returned an invalid JSON object contract",
                    "vista_invalid_contract",
                )
            return payload

        except urllib.error.HTTPError as exc:
            error_cat = f"http_{exc.code}"
            # A 400 while probing a documented candidate means this tenant does
            # not expose that field. It is contract negotiation, not downtime.
            if not (is_probe and exc.code == 400):
                self._record_failure(error_cat)
            raise VistaOrganizationAPIError(
                f"{endpoint_tag} request failed with HTTP {exc.code}",
                f"vista_http_{exc.code}",
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            self._record_failure("transport")
            raise VistaOrganizationAPIError(
                f"{endpoint_tag} request is unavailable due to transport error",
                "vista_transport_error",
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._record_failure("invalid_json")
            raise VistaOrganizationAPIError(
                f"{endpoint_tag} returned invalid JSON",
                "vista_invalid_json",
            ) from exc

    def _record_failure(self, error_category: str) -> None:
        if self._last_error_category == error_category:
            self._consecutive_failures += 1
        else:
            self._last_error_category = error_category
            self._consecutive_failures = 1

        if self._consecutive_failures >= self.max_failure_threshold:
            self._circuit_broken = True

    def _sanitize_user_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        user_id = self._extract_stable_id(record, "Codigo", "CodigoUsuario", "id")

        team_id, team_name_only = self._extract_relation(
            record,
            id_keys=[self.user_team_id_field, "CodigoEquipe", "Codigo_Equipe"],
            label_keys=[self.user_team_field, "Equipe", "NomeEquipe"],
        )
        manager_id, manager_name_only = self._extract_relation(
            record,
            id_keys=[self.user_manager_id_field, "CodigoGerente", "CodigoGestor", "Codigo_Gerente"],
            label_keys=[self.user_manager_field, "Gerente", "NomeGerente"],
        )
        agency_id, agency_name_only = self._extract_relation(
            record,
            id_keys=[self.user_agency_id_field, "CodigoAgencia", "CodigoLoja", "Codigo_Agencia"],
            label_keys=[self.user_agency_field, "Agencia", "Loja", "NomeAgencia"],
        )

        is_active, is_inactive, is_deleted = self._extract_lifecycle_flags(record)
        role_type = self._extract_role_type(record)

        return {
            "user_id": user_id,
            "role_type": role_type,
            "team_id": team_id,
            "manager_id": manager_id,
            "agency_id": agency_id,
            "team_name_only": team_name_only,
            "manager_name_only": manager_name_only,
            "agency_name_only": agency_name_only,
            "is_active": is_active,
            "is_inactive": is_inactive,
            "is_deleted": is_deleted,
        }

    def _sanitize_deal_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        deal_id = self._extract_stable_id(record, "Codigo", "deal_id")
        broker_id = self._extract_stable_id(
            record, "CorretorNegocio", "CodigoCorretor"
        )
        deal_date = str(record.get(self.created_field) or "")[:10] or None

        team_id, team_name_only = self._extract_relation(
            record,
            id_keys=[self.deal_team_id_field, "CodigoEquipe", "Codigo_Equipe"],
            label_keys=[self.deal_team_field, "EquipeNegocio", "Equipe"],
        )
        manager_id, manager_name_only = self._extract_relation(
            record,
            id_keys=[self.deal_manager_id_field, "CodigoGerente", "Codigo_Gerente"],
            label_keys=[self.deal_manager_field, "GerenteNegocio", "Gerente"],
        )
        agency_id, agency_name_only = self._extract_relation(
            record,
            id_keys=[self.deal_agency_id_field, "CodigoAgencia", "CodigoLoja", "Codigo_Agencia"],
            label_keys=[self.deal_agency_field, "AgenciaNegocio", "Agencia", "Loja"],
        )

        return {
            "deal_id": deal_id,
            "deal_date": deal_date,
            "broker_id": broker_id,
            "team_id": team_id,
            "manager_id": manager_id,
            "agency_id": agency_id,
            "team_name_only": team_name_only,
            "manager_name_only": manager_name_only,
            "agency_name_only": agency_name_only,
        }

    @staticmethod
    def _extract_relation(
        record: Dict[str, Any],
        id_keys: List[Optional[str]],
        label_keys: List[Optional[str]],
    ) -> Tuple[Optional[str], bool]:
        """Extract stable ID or flag as name_only_unverified.

        Rules:
        - Nested objects with 'Codigo' or 'id' provide a stable ID.
        - Fields matching explicit ID keys provide a stable ID.
        - Fields matching label keys containing plain text are NEVER promoted to ID;
          they produce name_only_unverified = True.
        """
        # 1. Check explicit ID fields first
        for key in id_keys:
            if not key:
                continue
            val = record.get(key)
            if isinstance(val, dict):
                val = val.get("Codigo") or val.get("id") or val.get("ID")
            if val not in (None, ""):
                cleaned = str(val).strip()
                if cleaned and cleaned.casefold() not in ("null", "none", "0", "sem_equipe"):
                    return cleaned, False

        # 2. Check label fields: if object has Codigo/id, accept it; if plain string, mark name_only
        for key in label_keys:
            if not key:
                continue
            val = record.get(key)
            if isinstance(val, dict):
                nested_id = val.get("Codigo") or val.get("id") or val.get("ID")
                if nested_id not in (None, ""):
                    cleaned = str(nested_id).strip()
                    if cleaned and cleaned.casefold() not in ("null", "none", "0", "sem_equipe"):
                        return cleaned, False
                nested_name = val.get("Nome") or val.get("Descricao")
                if nested_name not in (None, ""):
                    cleaned = str(nested_name).strip()
                    if cleaned and cleaned.casefold() not in ("null", "none", ""):
                        return None, True
            elif val not in (None, ""):
                cleaned = str(val).strip()
                if cleaned and cleaned.casefold() not in ("null", "none", "0", ""):
                    # String label is NEVER promoted to stable ID
                    return None, True

        return None, False

    @staticmethod
    def _extract_stable_id(record: Dict[str, Any], *keys: str) -> Optional[str]:
        for key in keys:
            if not key:
                continue
            val = record.get(key)
            if isinstance(val, dict):
                val = val.get("Codigo") or val.get("id") or val.get("ID")
            if val not in (None, ""):
                cleaned = str(val).strip()
                if cleaned and cleaned.casefold() not in (
                    "null",
                    "none",
                    "0",
                    "sem_equipe",
                ):
                    return cleaned
        return None

    @staticmethod
    def _extract_lifecycle_flags(
        record: Dict[str, Any]
    ) -> Tuple[bool, bool, bool]:
        status_val = (
            str(record.get("Status") or record.get("Situacao") or "")
            .strip()
            .casefold()
        )
        ativo_val = record.get("Ativo")
        is_deleted = bool(
            status_val in ("excluido", "deletado", "soft_deleted")
        )
        is_inactive = bool(
            is_deleted
            or status_val in (
                "inativo",
                "inativa",
                "bloqueado",
                "desativado",
                "inactive",
            )
            or ativo_val in (False, "Nao", "Não", "0", 0, "N", "false")
        )
        is_active = not is_inactive
        return is_active, is_inactive, is_deleted

    @staticmethod
    def _extract_role_type(record: Dict[str, Any]) -> str:
        """Categorize role strictly; returns 'unknown' when not recognized."""
        for key in ("Cargo", "Funcao", "Perfil"):
            val = str(record.get(key) or "").strip().casefold()
            if not val:
                continue
            if "gerente" in val or "gestor" in val or "coordenador" in val:
                return "manager"
            if "diretor" in val or "diretoria" in val or "executivo" in val:
                return "director"
            if (
                "corretor" in val
                or "vendedor" in val
                or "consultor" in val
                or "agente" in val
            ):
                return "broker"
            if "administrativo" in val or "secretaria" in val or "suporte" in val:
                return "staff"
        return "unknown"

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
