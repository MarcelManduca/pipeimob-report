import os
import uuid
import asyncio
import urllib.request
import socket
import json
import ssl
import time
from datetime import datetime, date, timezone, timedelta
from typing import List, Optional, Union, Any
from decimal import Decimal
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Header, Query, HTTPException, Response, Request, Depends, File, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from services.sales_reconciliation import (
    pipeimob_official_sale_date,
    reconcile_sales,
)
from services.vista_sales_client import (
    VistaSalesAPIError,
    VistaSalesClient,
    VistaSalesConfigurationError,
)

# Centralized HTTP Timeout configuration for external API requests (connection and read timeout)
try:
    _env_timeout = int(os.getenv("PIPEIMOB_HTTP_TIMEOUT_SECONDS", "12"))
    # Enforce bounds: min 1 second, max 30 seconds to stay compatible with frontend gateway timeouts
    PIPEIMOB_HTTP_TIMEOUT_SECONDS = max(1, min(30, _env_timeout))
except ValueError:
    PIPEIMOB_HTTP_TIMEOUT_SECONDS = 12

try:
    _env_request_attempts = int(os.getenv("PIPEIMOB_REQUEST_MAX_ATTEMPTS", "3"))
    PIPEIMOB_REQUEST_MAX_ATTEMPTS = max(1, min(5, _env_request_attempts))
except ValueError:
    PIPEIMOB_REQUEST_MAX_ATTEMPTS = 3

try:
    _env_retry_backoff = float(os.getenv("PIPEIMOB_RETRY_BACKOFF_SECONDS", "1"))
    PIPEIMOB_RETRY_BACKOFF_SECONDS = max(0.0, min(10.0, _env_retry_backoff))
except ValueError:
    PIPEIMOB_RETRY_BACKOFF_SECONDS = 1.0

# Token Cache in memory
class TokenCache:
    def __init__(self):
        self.access_token: Optional[str] = None
        self.token_type: str = "Bearer"
        self.expires_at: Optional[float] = None

token_cache = TokenCache()

# Dashboard Cache in memory (5 min TTL, configurable stale revalidation)
DASHBOARD_STALE_TTL_SECONDS = int(os.getenv("DASHBOARD_STALE_TTL_SECONDS", "3600"))

# Contracts Control temporary runtime authorization config
CONTRACTS_CONTROL_WRITES_ENABLED = os.getenv("CONTRACTS_CONTROL_WRITES_ENABLED", "false").lower() == "true"
_admin_subs_raw = os.getenv("CONTRACTS_CONTROL_ADMIN_SUBS", "")
CONTRACTS_CONTROL_ADMIN_SUBS = {s.strip() for s in _admin_subs_raw.split(",")} if _admin_subs_raw else set()


class DashboardCache:
    def __init__(self):
        from threading import Lock
        self.cache = {}
        self.lock = Lock()

    def get_status(self, key):
        with self.lock:
            if key in self.cache:
                val, fresh_until, stale_until = self.cache[key]
                now = time.time()
                if now <= fresh_until:
                    return val, "fresh"
                elif now <= stale_until:
                    return val, "stale"
                else:
                    del self.cache[key]
            return None, "miss"

    def get(self, key):
        val, status = self.get_status(key)
        return val

    def set(self, key, val, ttl=300):
        with self.lock:
            now = time.time()
            self.cache[key] = (val, now + ttl, now + ttl + DASHBOARD_STALE_TTL_SECONDS)

    def clear(self):
        with self.lock:
            self.cache.clear()

dashboard_cache = DashboardCache()
dashboard_cache.clear()

DASHBOARD_CACHE_VERSION = "v2"

class AsyncSingleFlightRegistry:
    def __init__(self):
        self.in_flight = {}  # key -> Task

    def get_or_create_task(self, key, fetch_coro):
        import logging
        import json
        logger = logging.getLogger(__name__)

        if key in self.in_flight:
            return self.in_flight[key]

        async def wrapper():
            try:
                return await fetch_coro()
            finally:
                current_task = asyncio.current_task()
                if self.in_flight.get(key) is current_task:
                    self.in_flight.pop(key, None)

        task = asyncio.create_task(wrapper())

        def done_callback(t):
            try:
                exc = t.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    logger.error(json.dumps({
                        "event": "singleflight_task_finished_exception",
                        "key": str(key),
                        "exception_class": type(exc).__name__,
                        "sanitized_message": str(exc)
                    }))
            except asyncio.CancelledError:
                logger.info(json.dumps({
                    "event": "singleflight_task_finished_cancelled",
                    "key": str(key)
                }))
            except Exception as cb_err:
                logger.error(json.dumps({
                    "event": "singleflight_callback_error",
                    "error": str(cb_err)
                }))

        task.add_done_callback(done_callback)
        self.in_flight[key] = task
        return task

    async def execute(self, key, fetch_coro, request_id=None, caller_endpoint=None, timeout=None):
        import time
        import json
        import logging
        logger = logging.getLogger(__name__)

        is_leader = key not in self.in_flight
        task = self.get_or_create_task(key, fetch_coro)
        role = "owner" if is_leader else "waiter"

        logger.info(json.dumps({
            "event": "singleflight_role_assigned",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "singleflight_role": role
        }))

        start_wait = time.perf_counter()
        logger.info(json.dumps({
            "event": "singleflight_wait_start",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "singleflight_role": role,
            "timeout_configured": timeout
        }))

        try:
            if timeout is not None:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            else:
                result = await asyncio.shield(task)

            wait_duration = (time.perf_counter() - start_wait) * 1000
            logger.info(json.dumps({
                "event": "singleflight_wait_end",
                "request_id": request_id,
                "caller_endpoint": caller_endpoint,
                "singleflight_role": role,
                "wait_duration_ms": wait_duration,
                "status": "success"
            }))
            return result
        except asyncio.TimeoutError as te:
            wait_duration = (time.perf_counter() - start_wait) * 1000
            logger.info(json.dumps({
                "event": "singleflight_wait_end",
                "request_id": request_id,
                "caller_endpoint": caller_endpoint,
                "singleflight_role": role,
                "wait_duration_ms": wait_duration,
                "status": "timeout"
            }))
            raise te
        except asyncio.CancelledError as ce:
            wait_duration = (time.perf_counter() - start_wait) * 1000
            logger.info(json.dumps({
                "event": "singleflight_wait_end",
                "request_id": request_id,
                "caller_endpoint": caller_endpoint,
                "singleflight_role": role,
                "wait_duration_ms": wait_duration,
                "status": "cancelled"
            }))
            raise ce
        except Exception as err:
            wait_duration = (time.perf_counter() - start_wait) * 1000
            logger.info(json.dumps({
                "event": "singleflight_wait_end",
                "request_id": request_id,
                "caller_endpoint": caller_endpoint,
                "singleflight_role": role,
                "wait_duration_ms": wait_duration,
                "status": "failure",
                "exception_class": type(err).__name__,
                "sanitized_message": str(err)
            }))
            raise err

    def start_background(self, key, fetch_coro):
        self.get_or_create_task(key, fetch_coro)

    async def is_running(self, key):
        return key in self.in_flight

single_flight_registry = AsyncSingleFlightRegistry()

def generate_dashboard_cache_key(
    data_inicio_criacao: Optional[str] = None,
    data_fim_criacao: Optional[str] = None,
    data_inicio_ccv: Optional[str] = None,
    data_fim_ccv: Optional[str] = None,
    data_arquivamento_inicio: Optional[str] = None,
    data_arquivamento_fim: Optional[str] = None,
    codigo_imovel: Optional[str] = None,
    codigo_contrato: Optional[str] = None,
    transacao_unique_id: Optional[str] = None,
    requested_granularity: Optional[str] = None,
    applied_granularity: Optional[str] = None
) -> tuple:
    req_g = requested_granularity or "month"
    app_g = applied_granularity or req_g
    return (
        "bi",
        DASHBOARD_CACHE_VERSION,
        data_inicio_criacao, data_fim_criacao,
        data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim,
        codigo_imovel, codigo_contrato, transacao_unique_id,
        req_g, app_g
    )

def parse_official_team_groups() -> tuple[str, bool, dict, list[str]]:
    # Returns (configuration_status, official_teams_configured, group_mapping, official_teams)
    raw = os.getenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON")
    if raw is None or raw.strip() == "":
        return "missing", False, {}, []

    try:
        data = json.loads(raw)
    except Exception:
        return "invalid", False, {}, []

    if not isinstance(data, list):
        return "invalid", False, {}, []

    if len(data) == 0:
        return "incomplete", False, {}, []

    group_mapping = {}
    team_names = set()
    ids_seen = set()
    has_at_least_one_team = False

    for entry in data:
        if not isinstance(entry, dict):
            return "incomplete", False, {}, []
        gid = entry.get("id")
        name = entry.get("name")
        gtype = entry.get("type")

        if gid is None or name is None or gtype is None:
            return "incomplete", False, {}, []

        gid = str(gid).strip()
        name = str(name).strip()
        gtype = str(gtype).strip()

        if gid == "" or name == "" or gtype == "":
            return "incomplete", False, {}, []

        if gtype not in ["team", "branch", "other"]:
            return "incomplete", False, {}, []

        if gid in ids_seen:
            return "incomplete", False, {}, []
        ids_seen.add(gid)

        if gtype == "team":
            has_at_least_one_team = True
            normalized_name = " ".join(name.split()).lower()
            if normalized_name in team_names:
                return "incomplete", False, {}, []
            team_names.add(normalized_name)

        group_mapping[gid] = {
            "name": name,
            "type": gtype
        }

    if not has_at_least_one_team:
        return "incomplete", False, {}, []

    official_teams = [g["name"] for g in group_mapping.values() if g["type"] == "team"]
    return "configured", True, group_mapping, sorted(official_teams)

# Mock data will be imported locally inside load_transactions_dataset to avoid any global access.

# Load environment variables
load_dotenv()

app = FastAPI(
    title="Pipeimob Report API",
    description=(
        "Backend API for Pipeimob reports and Lovable integration (BI Dashboard - Phase 2).\n\n"
        "### Especificações Técnicas da Integração Pipeimob CRM:\n"
        "- **Autenticação:** Realizada via JWT no endpoint `POST /api/v2/auth`. O token de acesso é extraído de `data.access_token` e fornecido via header `Authorization: Bearer <token>` em chamadas subsequentes.\n"
        "- **Cache de Token:** O token JWT é mantido em cache de memória server-side e renovado automaticamente com uma margem de segurança de 60 segundos antes de expirar.\n"
        "- **Parâmetro de Busca:** O parâmetro de busca por ID de transação é `transacao_unique_id`, enquanto a chave de identificação do registro retornada no payload é `transacao_unique_id_pipeimob`.\n"
        "- **Paginação:** Feita exclusivamente via parâmetro de query `pagina` com tamanho de página fixo de 25 registros.\n"
        "- **Filtros Obrigatórios:** A API do Pipeimob exige pelo menos um filtro direto nas consultas Live (ex.: data de criação, CCV, arquivamento, códigos específicos ou transacao_unique_id). Chamadas live sem filtro direto retornam HTTP 400.\n"
        "- **Filtros Locais:** Os filtros por gestor (`agent`), categoria (`category`), financiamento (`financing`) e etapa (`etapa_atual`) são processados localmente pelo backend.\n"
        "- **Comissões:** A métrica oficial do VGC total é `total_comissao`, enquanto a `comissao_imobiliaria` é calculada somando os comissionados com flag comissionado_imobiliária como true."
    ),
    version="0.1.0",
)

class IntegrationUnavailableError(Exception):
    def __init__(self, status_code: int, detail: str, error_code: str, data_mode: str, pipeimob_connection: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code
        self.data_mode = data_mode
        self.pipeimob_connection = pipeimob_connection

@app.exception_handler(IntegrationUnavailableError)
async def integration_unavailable_exception_handler(request: Request, exc: IntegrationUnavailableError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error_code": exc.error_code,
            "data_mode": exc.data_mode,
            "pipeimob_connection": exc.pipeimob_connection
        }
    )

from services.contracts_control_exceptions import InvalidSpreadsheetError, DatasetWarmingError

@app.exception_handler(InvalidSpreadsheetError)
async def invalid_spreadsheet_exception_handler(request: Request, exc: InvalidSpreadsheetError):
    return JSONResponse(
        status_code=400,
        content={
            "code": "invalid_spreadsheet",
            "detail": "O arquivo XLSX é inválido ou está corrompido."
        }
    )

@app.exception_handler(DatasetWarmingError)
async def dataset_warming_exception_handler(request: Request, exc: DatasetWarmingError):
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(exc.retry_after)},
        content={
            "code": "dataset_warming",
            "detail": "Os dados do Pipeimob estão sendo preparados. Tente novamente em instantes."
        }
    )

class AuthException(Exception):
    def __init__(self, status_code: int, detail: str, error_code: str):
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code

@app.exception_handler(AuthException)
async def auth_exception_handler(request: Request, exc: AuthException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error_code": exc.error_code
        }
    )

class GranularityException(Exception):
    def __init__(self, status_code: int, detail: str, error_code: str):
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code

@app.exception_handler(GranularityException)
async def granularity_exception_handler(request: Request, exc: GranularityException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error_code": exc.error_code
        }
    )

# CORS Configuration
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins: List[str] = []

if allowed_origins_env:
    allowed_origins = [orig.strip() for orig in allowed_origins_env.split(",") if orig.strip()]

app_env = os.getenv("APP_ENV", "production").lower()

if app_env == "development":
    dev_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    for orig in dev_origins:
        if orig not in allowed_origins:
            allowed_origins.append(orig)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://.*--happy-data-hugger\.lovable\.app",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "Origin"],
)

ssl_context = ssl._create_unverified_context()
BASE_URL = "https://api.pipeimob.com.br/api/v2"

# Authentication helper
def get_auth_token(api_key: str, api_secret: str, force_refresh: bool = False) -> Optional[str]:
    global token_cache
    now = time.time()
    if not force_refresh and token_cache.access_token and token_cache.expires_at and now < token_cache.expires_at:
        return token_cache.access_token

    url = f"{BASE_URL}/auth"
    payload = {
        "api_key": api_key,
        "api_secret": api_secret
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    try:
        # Use a short timeout to handle hanging requests
        with urllib.request.urlopen(req, context=ssl_context, timeout=min(8, PIPEIMOB_HTTP_TIMEOUT_SECONDS)) as response:
            res_body = json.loads(response.read().decode('utf-8'))
            if res_body.get("success"):
                data = res_body.get("data") or {}
                token = data.get("access_token")
                if not token or not isinstance(token, str) or not token.strip():
                    raise IntegrationUnavailableError(
                        status_code=503,
                        detail="Authentication succeeded but access_token is empty or invalid.",
                        error_code="authentication_failed",
                        data_mode="live",
                        pipeimob_connection="authentication_failed"
                    )
                expires_in = data.get("expires_in") or 3600
                token_type = data.get("token_type") or "Bearer"

                # Cache token - with a margin of 60 seconds
                token_cache.access_token = token
                token_cache.token_type = token_type
                token_cache.expires_at = now + float(expires_in) - 60.0
                return token
            else:
                raise IntegrationUnavailableError(
                    status_code=503,
                    detail="Authentication payload returned success=False. Invalid API Key or Secret Key.",
                    error_code="authentication_failed",
                    data_mode="live",
                    pipeimob_connection="authentication_failed"
                )
    except urllib.error.HTTPError as e:
        if e.code in [401, 403]:
            raise IntegrationUnavailableError(
                status_code=503,
                detail=f"Failed to authenticate with Pipeimob CRM API (HTTP {e.code}).",
                error_code="authentication_failed",
                data_mode="live",
                pipeimob_connection="authentication_failed"
            )
        else:
            raise IntegrationUnavailableError(
                status_code=503,
                detail=f"Pipeimob API is temporarily unavailable (HTTP {e.code}).",
                error_code="pipeimob_unavailable",
                data_mode="live",
                pipeimob_connection="unavailable"
            )
    except urllib.error.URLError as e:
        is_timeout = "timeout" in str(e.reason).lower() if hasattr(e, 'reason') else False
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Pipeimob CRM Authentication request timed out." if is_timeout else "Pipeimob CRM Authentication is unreachable.",
            error_code="pipeimob_auth_timeout" if is_timeout else "pipeimob_auth_unavailable",
            data_mode="live",
            pipeimob_connection="unavailable"
        )
    except Exception as e:
        if isinstance(e, IntegrationUnavailableError):
            raise e
        raise IntegrationUnavailableError(
            status_code=503,
            detail=f"Invalid response format from Pipeimob: {e}",
            error_code="invalid_pipeimob_response",
            data_mode="live",
            pipeimob_connection="unavailable"
        )

# Live transaction sequential page fetcher
def fetch_all_pipeimob_transactions(
    api_key: str,
    api_secret: str,
    data_inicio_criacao: Optional[str] = None,
    data_fim_criacao: Optional[str] = None,
    data_inicio_ccv: Optional[str] = None,
    data_fim_ccv: Optional[str] = None,
    data_arquivamento_inicio: Optional[str] = None,
    data_arquivamento_fim: Optional[str] = None,
    codigo_imovel: Optional[str] = None,
    codigo_contrato: Optional[str] = None,
    transacao_unique_id: Optional[str] = None
) -> tuple:
    token = get_auth_token(api_key, api_secret)
    if not token:
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Authentication succeeded but failed to retrieve access token.",
            error_code="authentication_failed",
            data_mode="live",
            pipeimob_connection="authentication_failed"
        )

    query_parts = []
    if data_inicio_criacao: query_parts.append(f"data_inicio_criacao={data_inicio_criacao}")
    if data_fim_criacao: query_parts.append(f"data_fim_criacao={data_fim_criacao}")
    if data_inicio_ccv: query_parts.append(f"data_inicio_ccv={data_inicio_ccv}")
    if data_fim_ccv: query_parts.append(f"data_fim_ccv={data_fim_ccv}")
    if data_arquivamento_inicio: query_parts.append(f"data_arquivamento_inicio={data_arquivamento_inicio}")
    if data_arquivamento_fim: query_parts.append(f"data_arquivamento_fim={data_arquivamento_fim}")
    if codigo_imovel: query_parts.append(f"codigo_imovel={codigo_imovel}")
    if codigo_contrato: query_parts.append(f"codigo_contrato={codigo_contrato}")
    if transacao_unique_id: query_parts.append(f"transacao_unique_id={transacao_unique_id}")

    query_str = "&".join(query_parts)
    prefix = f"&{query_str}" if query_str else ""

    def request_with_retry(
        url: str,
        auth_retry_allowed: bool = True,
        attempt: int = 1,
    ) -> dict:
        nonlocal token
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {token}', 'User-Agent': 'Mozilla/5.0'}
        )
        try:
            with urllib.request.urlopen(req, context=ssl_context, timeout=PIPEIMOB_HTTP_TIMEOUT_SECONDS) as response:
                raw_body = response.read().decode('utf-8')
                try:
                    res_body = json.loads(raw_body)
                except Exception as json_err:
                    raise IntegrationUnavailableError(
                        status_code=503,
                        detail=f"Invalid JSON format from Pipeimob: {json_err}",
                        error_code="invalid_pipeimob_response",
                        data_mode="live",
                        pipeimob_connection="unavailable"
                    )

                if not isinstance(res_body, dict):
                    raise IntegrationUnavailableError(
                        status_code=503,
                        detail="Pipeimob response is not a valid JSON object.",
                        error_code="invalid_pipeimob_response",
                        data_mode="live",
                        pipeimob_connection="unavailable"
                    )

                if res_body.get("success") is not True:
                    raise IntegrationUnavailableError(
                        status_code=503,
                        detail="Pipeimob transactions API returned success=False or missing success field.",
                        error_code="invalid_pipeimob_response",
                        data_mode="live",
                        pipeimob_connection="unavailable"
                    )
                return res_body
        except urllib.error.HTTPError as e:
            if e.code == 401 and auth_retry_allowed:
                token = get_auth_token(api_key, api_secret, force_refresh=True)
                return request_with_retry(
                    url,
                    auth_retry_allowed=False,
                    attempt=attempt,
                )
            if (
                e.code in {408, 429, 500, 502, 503, 504}
                and attempt < PIPEIMOB_REQUEST_MAX_ATTEMPTS
            ):
                time.sleep(PIPEIMOB_RETRY_BACKOFF_SECONDS * attempt)
                return request_with_retry(
                    url,
                    auth_retry_allowed=auth_retry_allowed,
                    attempt=attempt + 1,
                )
            detail = (
                f"Pipeimob API remained unavailable (HTTP {e.code}) "
                f"after {attempt} attempts."
                if e.code in {408, 429, 500, 502, 503, 504}
                else f"Pipeimob API is temporarily unavailable (HTTP {e.code})."
            )
            raise IntegrationUnavailableError(
                status_code=503,
                detail=detail,
                error_code="pipeimob_unavailable",
                data_mode="live",
                pipeimob_connection="unavailable"
            )
        except urllib.error.URLError as e:
            is_timeout = "timeout" in str(e.reason).lower() if hasattr(e, 'reason') else False
            if is_timeout and attempt < PIPEIMOB_REQUEST_MAX_ATTEMPTS:
                time.sleep(PIPEIMOB_RETRY_BACKOFF_SECONDS * attempt)
                return request_with_retry(
                    url,
                    auth_retry_allowed=auth_retry_allowed,
                    attempt=attempt + 1,
                )
            raise IntegrationUnavailableError(
                status_code=503,
                detail=(
                    f"Pipeimob CRM Pagination request timed out after {attempt} attempts."
                    if is_timeout
                    else "Pipeimob CRM Pagination is unreachable."
                ),
                error_code="pipeimob_pagination_timeout" if is_timeout else "pipeimob_pagination_unavailable",
                data_mode="live",
                pipeimob_connection="unavailable"
            )
        except (TimeoutError, socket.timeout):
            if attempt < PIPEIMOB_REQUEST_MAX_ATTEMPTS:
                time.sleep(PIPEIMOB_RETRY_BACKOFF_SECONDS * attempt)
                return request_with_retry(
                    url,
                    auth_retry_allowed=auth_retry_allowed,
                    attempt=attempt + 1,
                )
            raise IntegrationUnavailableError(
                status_code=503,
                detail=f"Pipeimob CRM Pagination request timed out after {attempt} attempts.",
                error_code="pipeimob_pagination_timeout",
                data_mode="live",
                pipeimob_connection="unavailable"
            )
        except Exception as e:
            if isinstance(e, IntegrationUnavailableError):
                raise e
            raise IntegrationUnavailableError(
                status_code=503,
                detail=f"Invalid response format from Pipeimob: {e}",
                error_code="invalid_pipeimob_response",
                data_mode="live",
                pipeimob_connection="unavailable"
            )

    all_transactions = []
    seen_ids = set()
    current_page = 1
    pages_fetched = 0

    while True:
        if current_page > 100:  # Infinite loop protection
            break

        url = f"{BASE_URL}/negocios/transacoes?pagina={current_page}{prefix}"
        res_body = request_with_retry(url)
        pages_fetched += 1

        data_obj = res_body.get("data")
        if not isinstance(data_obj, dict):
            raise IntegrationUnavailableError(
                status_code=503,
                detail="Pipeimob response data field is missing or not an object.",
                error_code="invalid_pipeimob_response",
                data_mode="live",
                pipeimob_connection="unavailable"
            )

        if "transacoes" not in data_obj or not isinstance(data_obj.get("transacoes"), list):
            raise IntegrationUnavailableError(
                status_code=503,
                detail="Pipeimob response transacoes field is missing or not a list.",
                error_code="invalid_pipeimob_response",
                data_mode="live",
                pipeimob_connection="unavailable"
            )

        txs = data_obj["transacoes"]

        meta_p = None
        if "meta" in res_body and isinstance(res_body["meta"], dict) and "pagination" in res_body["meta"] and isinstance(res_body["meta"]["pagination"], dict):
            meta_p = res_body["meta"]["pagination"]
        elif "meta" in data_obj and isinstance(data_obj["meta"], dict) and "pagination" in data_obj["meta"] and isinstance(data_obj["meta"]["pagination"], dict):
            meta_p = data_obj["meta"]["pagination"]

        if meta_p is None:
            raise IntegrationUnavailableError(
                status_code=503,
                detail="Pagination metadata not found in Pipeimob response.",
                error_code="invalid_pipeimob_response",
                data_mode="live",
                pipeimob_connection="unavailable"
            )

        total = meta_p.get("total")

        # Strict pagination coherency validation
        if current_page == 1:
            if len(txs) == 0 and total is not None and total > 0:
                raise IntegrationUnavailableError(
                    status_code=503,
                    detail="Pipeimob pagination metadata total > 0 but transacoes array is empty.",
                    error_code="invalid_pipeimob_response",
                    data_mode="live",
                    pipeimob_connection="unavailable"
                )
            if len(txs) > 0 and total == 0:
                raise IntegrationUnavailableError(
                    status_code=503,
                    detail="Pipeimob pagination metadata total == 0 but transacoes array has items.",
                    error_code="invalid_pipeimob_response",
                    data_mode="live",
                    pipeimob_connection="unavailable"
                )

        for tx in txs:
            if not isinstance(tx, dict):
                raise IntegrationUnavailableError(
                    status_code=503,
                    detail="Transaction element in Pipeimob response is not a valid dictionary.",
                    error_code="invalid_pipeimob_response",
                    data_mode="live",
                    pipeimob_connection="unavailable"
                )
            tx_id = tx.get("transacao_unique_id_pipeimob")
            if tx_id:
                if tx_id not in seen_ids:
                    seen_ids.add(tx_id)
                    all_transactions.append(tx)
            else:
                all_transactions.append(tx)

        if total == 0:
            break

        last_page = meta_p.get("total_pages") or 1

        if current_page >= last_page:
            break

        current_page += 1

    return all_transactions, pages_fetched

def get_current_data_mode_and_connection() -> tuple:
    data_mode_env = os.getenv("PIPEIMOB_DATA_MODE")
    app_env = os.getenv("APP_ENV", "production").lower()

    api_key = os.getenv("PIPEIMOB_API_KEY")
    api_secret = os.getenv("PIPEIMOB_SECRET_KEY")
    has_credentials = bool(api_key and api_secret)

    if data_mode_env == "demo":
        return "demo", "not_tested"
    elif data_mode_env == "live":
        if has_credentials:
            return "live", "configured"
        else:
            return "live", "missing_credentials"
    elif not data_mode_env:
        if app_env == "development":
            return "demo", "not_tested"
        else:
            return "unconfigured", "pending_configuration"
    else:
        return "unconfigured", "pending_configuration"

def validate_dataset_origin(mode: str, source: str, dataset: list):
    app_env = os.getenv("APP_ENV", "production").lower()
    data_mode_env = os.getenv("PIPEIMOB_DATA_MODE")

    # 1. Production + Live check
    if app_env == "production" and data_mode_env == "live":
        if mode != "live" or source != "pipeimob_api_v2":
            raise HTTPException(
                status_code=500,
                detail="Critical failure: Live mode in production cannot use mock data or non-live source."
            )

    # 2. Strict matching rules
    if mode == "live":
        if source != "pipeimob_api_v2":
            raise HTTPException(
                status_code=500,
                detail="Data source mismatch: Live mode requires 'pipeimob_api_v2' source."
            )
        # Ensure no mock transaction IDs exist in live dataset
        for tx in dataset:
            tx_id = str(tx.get("transacao_unique_id_pipeimob") or "")
            if tx_id.startswith("tx_demo_"):
                raise HTTPException(
                    status_code=500,
                    detail="Critical security policy violation: Mock data detected in live dataset."
                )
    elif mode == "demo":
        if source != "synthetic_mock":
            raise HTTPException(
                status_code=500,
                detail="Data source mismatch: Demo mode requires 'synthetic_mock' source."
            )
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Unsupported active data mode: {mode}"
        )

def get_receipt_date(tx: dict) -> tuple[Optional[str], Optional[str]]:
    d_prev = tx.get("data_pagamento_comissao_prevista")
    if d_prev is not None and str(d_prev).strip() != "":
        return str(d_prev).strip(), "data_pagamento_comissao_prevista"

    d_rec = tx.get("data_recebimento_comissao")
    if d_rec is not None and str(d_rec).strip() != "":
        return str(d_rec).strip(), "data_recebimento_comissao"

    return None, None

def parse_explicit_date(date_str: str):
    if not date_str:
        return None
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo
    date_str = date_str.strip()

    # 1. DD/MM/YYYY
    if re.match(r"^\d{2}/\d{2}/\d{4}$", date_str):
        try:
            return datetime.strptime(date_str, "%d/%m/%Y").date()
        except ValueError:
            return None

    # 2. YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None

    # 3. ISO 8601 with date and time
    if "T" in date_str or (" " in date_str and len(date_str) > 10):
        iso_str = date_str.replace("Z", "+00:00")
        try:
            # We match timezone suffix like +HH:MM, -HH:MM or +00:00.
            # If no offset is present, treat as naive and assume local CRM time (America/Sao_Paulo).
            has_tz = re.search(r"([+-]\d{2}:?\d{2}|Z)$", date_str) or "+00:00" in iso_str
            dt = datetime.fromisoformat(iso_str)
            if not has_tz or dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
            else:
                dt = dt.astimezone(ZoneInfo("America/Sao_Paulo"))
            return dt.date()
        except ValueError:
            return None

    return None

def calculate_percentile(sorted_values: list, percentile: float) -> float:
    """
    Calculates a percentile using linear interpolation between closest ranks (inclusive method).
    Reference formula:
    idx = percentile * (N - 1)
    low = floor(idx)
    high = ceil(idx)
    percentile_value = V[low] + (idx - low) * (V[high] - V[low])
    """
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return float(sorted_values[0])

    idx = percentile * (n - 1)
    low = int(idx)
    high = low + 1
    if high >= n:
        return float(sorted_values[low])

    d = idx - low
    val = sorted_values[low] + d * (sorted_values[high] - sorted_values[low])
    return round(val, 1)


def to_decimal(val) -> Decimal:
    if isinstance(val, (int, Decimal)):
        return Decimal(val)
    if isinstance(val, float):
        return Decimal(str(val))
    val_str = str(val).strip()
    if not val_str:
        raise ValueError("Empty value")
    val_str = val_str.replace("R$", "").strip()
    if "," in val_str:
        if "." in val_str:
            val_str = val_str.replace(".", "")
        val_str = val_str.replace(",", ".")
    return Decimal(val_str)

class CommissionSplitExtraction:
    def __init__(
        self,
        gralha_amount: Optional[Decimal],
        all_participants_amount: Optional[Decimal],
        status: str,
        matching_company_items_count: int,
        items_count: int
    ):
        self.gralha_amount = gralha_amount
        self.all_participants_amount = all_participants_amount
        self.status = status
        self.matching_company_items_count = matching_company_items_count
        self.items_count = items_count

def extract_commission_split(tx: dict) -> CommissionSplitExtraction:
    comissionados = tx.get("comissionados")

    total_comm_raw = tx.get("total_comissao")
    if total_comm_raw is None or str(total_comm_raw).strip() == "":
        total_comissao = Decimal("0")
    else:
        try:
            total_comissao = to_decimal(total_comm_raw)
        except Exception:
            total_comissao = Decimal("0")

    if comissionados is None:
        return CommissionSplitExtraction(
            gralha_amount=None,
            all_participants_amount=None,
            status="missing_array",
            matching_company_items_count=0,
            items_count=0
        )

    if not isinstance(comissionados, list):
        return CommissionSplitExtraction(
            gralha_amount=None,
            all_participants_amount=None,
            status="malformed_array",
            matching_company_items_count=0,
            items_count=0
        )

    gralha_amount = Decimal("0")
    all_participants_amount = Decimal("0")
    matching_company_items_count = 0
    items_count = len(comissionados)

    for item in comissionados:
        if not isinstance(item, dict):
            return CommissionSplitExtraction(
                gralha_amount=None,
                all_participants_amount=None,
                status="invalid_value",
                matching_company_items_count=0,
                items_count=items_count
            )

        val_raw = item.get("comissionado_valor")
        if val_raw is None or str(val_raw).strip() == "":
            return CommissionSplitExtraction(
                gralha_amount=None,
                all_participants_amount=None,
                status="invalid_value",
                matching_company_items_count=0,
                items_count=items_count
            )

        try:
            val = to_decimal(val_raw)
            if val < 0:
                return CommissionSplitExtraction(
                    gralha_amount=None,
                    all_participants_amount=None,
                    status="invalid_value",
                    matching_company_items_count=0,
                    items_count=items_count
                )
        except Exception:
            return CommissionSplitExtraction(
                gralha_amount=None,
                all_participants_amount=None,
                status="invalid_value",
                matching_company_items_count=0,
                items_count=items_count
            )

        all_participants_amount += val

        is_imob = item.get("comissionado_imobiliária")
        if is_imob is None:
            is_imob = item.get("comissionado_imobiliaria")

        is_filial = item.get("comissionado_filial")

        is_imob_bool = is_imob is True or str(is_imob).lower() in ["true", "1"]
        is_filial_bool = is_filial is True or str(is_filial).lower() in ["true", "1"]

        if is_imob_bool or is_filial_bool:
            gralha_amount += val
            matching_company_items_count += 1

    diff = abs(all_participants_amount - total_comissao)
    if diff > Decimal("0.01"):
        return CommissionSplitExtraction(
            gralha_amount=gralha_amount,
            all_participants_amount=all_participants_amount,
            status="reconciliation_mismatch",
            matching_company_items_count=matching_company_items_count,
            items_count=items_count
        )

    return CommissionSplitExtraction(
        gralha_amount=gralha_amount,
        all_participants_amount=all_participants_amount,
        status="valid",
        matching_company_items_count=matching_company_items_count,
        items_count=items_count
    )

def calculate_vgc_split(tx: dict) -> tuple[Decimal, Decimal, Decimal]:
    total_comm_raw = tx.get("total_comissao")
    if total_comm_raw is None or str(total_comm_raw).strip() == "":
        vgc_total = Decimal("0")
    else:
        try:
            vgc_total = to_decimal(total_comm_raw)
        except Exception:
            vgc_total = Decimal("0")

    ext = extract_commission_split(tx)
    if ext.status == "valid":
        vgc_gralha = ext.gralha_amount
        vgc_demais_participantes = vgc_total - vgc_gralha
    else:
        vgc_gralha = Decimal("0")
        vgc_demais_participantes = Decimal("0")

    return vgc_total, vgc_gralha, vgc_demais_participantes

async def load_transactions_dataset(
    data_inicio_criacao: Optional[str] = None,
    data_fim_criacao: Optional[str] = None,
    data_inicio_ccv: Optional[str] = None,
    data_fim_ccv: Optional[str] = None,
    data_arquivamento_inicio: Optional[str] = None,
    data_arquivamento_fim: Optional[str] = None,
    codigo_imovel: Optional[str] = None,
    codigo_contrato: Optional[str] = None,
    transacao_unique_id: Optional[str] = None,
    pagina: Optional[int] = None,
    request_id: Optional[str] = None,
    refresh: bool = False,
    requested_granularity: Optional[str] = None,
    applied_granularity: Optional[str] = None
) -> tuple:
    import time
    start_time = time.perf_counter()
    data_mode, conn_status = get_current_data_mode_and_connection()

    if data_mode == "unconfigured":
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Configuration pending. Please set PIPEIMOB_DATA_MODE environment variable.",
            error_code="integration_unconfigured",
            data_mode="unconfigured",
            pipeimob_connection="pending_configuration"
        )

    if data_mode == "demo":
        from mock_data import MOCK_TRANSACTIONS
        dataset = MOCK_TRANSACTIONS

        duration_ms = (time.perf_counter() - start_time) * 1000
        log_msg = {
            "event": "performance_metric",
            "request_id": request_id or "unknown",
            "periodo": {
                "data_inicio_ccv": data_inicio_ccv,
                "data_fim_ccv": data_fim_ccv,
                "data_inicio_criacao": data_inicio_criacao,
                "data_fim_criacao": data_fim_criacao
            },
            "paginas_consultadas": 1,
            "quantidade_transacoes": len(dataset),
            "cache_hit": False,
            "processing_time_ms": round(duration_ms, 2),
            "cache_status": "miss"
        }
        print(f"SECURE_LOG: {json.dumps(log_msg)}")
        return "demo", "synthetic_mock", dataset, 1, "miss"

    # Live mode: validate that at least one direct filter is present.
    has_direct_filter = any([
        data_inicio_criacao,
        data_fim_criacao,
        data_inicio_ccv,
        data_fim_ccv,
        data_arquivamento_inicio,
        data_arquivamento_fim,
        codigo_imovel,
        codigo_contrato,
        transacao_unique_id
    ])

    if not has_direct_filter:
        raise HTTPException(
            status_code=400,
            detail="At least one direct filter parameter is required in Live mode: data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id."
        )

    if conn_status == "missing_credentials":
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Pipeimob credentials are not configured on the server.",
            error_code="missing_credentials",
            data_mode="live",
            pipeimob_connection="missing_credentials"
        )

    cache_key = generate_dashboard_cache_key(
        data_inicio_criacao, data_fim_criacao,
        data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim,
        codigo_imovel, codigo_contrato, transacao_unique_id,
        requested_granularity, applied_granularity
    )

    def sync_fetch():
        api_key = os.getenv("PIPEIMOB_API_KEY").strip()
        api_secret = os.getenv("PIPEIMOB_SECRET_KEY").strip()

        txs, pages = fetch_all_pipeimob_transactions(
            api_key=api_key,
            api_secret=api_secret,
            data_inicio_criacao=data_inicio_criacao,
            data_fim_criacao=data_fim_criacao,
            data_inicio_ccv=data_inicio_ccv,
            data_fim_ccv=data_fim_ccv,
            data_arquivamento_inicio=data_arquivamento_inicio,
            data_arquivamento_fim=data_arquivamento_fim,
            codigo_imovel=codigo_imovel,
            codigo_contrato=codigo_contrato,
            transacao_unique_id=transacao_unique_id
        )

        for tx in txs:
            _, vgc_gralha, _ = calculate_vgc_split(tx)
            tx["comissao_imobiliaria"] = float(vgc_gralha)
            if "data_recebimento_comissao" not in tx or tx.get("data_recebimento_comissao") is None:
                tx["data_recebimento_comissao"] = tx.get("data_pagamento_comissao")

        dashboard_cache.set(cache_key, (txs, pages))
        return txs, pages

    # SWR and Single-Flight check
    if refresh:
        coro = lambda: asyncio.get_event_loop().run_in_executor(None, sync_fetch)
        live_txs, pages_fetched = await single_flight_registry.execute(cache_key, coro)
        cache_status = "miss"
    else:
        cached_val, status = dashboard_cache.get_status(cache_key)
        if status == "fresh":
            live_txs, pages_fetched = cached_val
            cache_status = "fresh"
        elif status == "stale":
            live_txs, pages_fetched = cached_val
            cache_status = "stale"
            if not await single_flight_registry.is_running(cache_key):
                async def run_bg_refresh():
                    try:
                        c = lambda: asyncio.get_event_loop().run_in_executor(None, sync_fetch)
                        await single_flight_registry.execute(cache_key, c)
                    except Exception:
                        pass
                asyncio.create_task(run_bg_refresh())
        else:
            coro = lambda: asyncio.get_event_loop().run_in_executor(None, sync_fetch)
            live_txs, pages_fetched = await single_flight_registry.execute(cache_key, coro)
            cache_status = "miss"

    txs_to_return = live_txs
    if pagina is not None:
        start_idx = (pagina - 1) * 25
        end_idx = start_idx + 25
        txs_to_return = live_txs[start_idx:end_idx]

    duration_ms = (time.perf_counter() - start_time) * 1000
    log_msg = {
        "event": "performance_metric",
        "request_id": request_id or "unknown",
        "periodo": {
            "data_inicio_ccv": data_inicio_ccv,
            "data_fim_ccv": data_fim_ccv,
            "data_inicio_criacao": data_inicio_criacao,
            "data_fim_criacao": data_fim_criacao
        },
        "paginas_consultadas": pages_fetched,
        "quantidade_transacoes": len(txs_to_return),
        "cache_hit": (cache_status in ["fresh", "stale"]),
        "cache_status": cache_status,
        "processing_time_ms": round(duration_ms, 2)
    }
    print(f"SECURE_LOG: {json.dumps(log_msg)}")
    return "live", "pipeimob_api_v2", txs_to_return, pages_fetched, cache_status
# Apply filters locally on loaded dataset
def get_filtered_transactions(
    transactions: list,
    data_mode: str,
    data_inicio_criacao: Optional[str] = None,
    data_fim_criacao: Optional[str] = None,
    data_inicio_ccv: Optional[str] = None,
    data_fim_ccv: Optional[str] = None,
    data_arquivamento_inicio: Optional[str] = None,
    data_arquivamento_fim: Optional[str] = None,
    codigo_imovel: Optional[str] = None,
    codigo_contrato: Optional[str] = None,
    transacao_unique_id: Optional[str] = None,
    agent: Optional[str] = None,
    category: Optional[str] = None,
    financing: Optional[bool] = None,
    etapa_atual: Optional[str] = None
) -> list:
    filtered = []
    for tx in transactions:
        # In demo mode, apply period filters locally for fully functional mock visualization
        if data_mode == "demo":
            tx_date_str = tx.get("data_inicio_venda") or tx.get("data_contrato") or ""
            if data_inicio_criacao and tx_date_str and tx_date_str < data_inicio_criacao:
                continue
            if data_fim_criacao and tx_date_str and tx_date_str > data_fim_criacao:
                continue

            tx_ccv = tx.get("data_contrato") or ""
            if data_inicio_ccv and tx_ccv and tx_ccv < data_inicio_ccv:
                continue
            if data_fim_ccv and tx_ccv and tx_ccv > data_fim_ccv:
                continue

            if tx.get("etapa_atual") == "Arquivado":
                tx_archived = tx.get("data_contrato") or ""
                if data_arquivamento_inicio and tx_archived and tx_archived < data_arquivamento_inicio:
                    continue
                if data_arquivamento_fim and tx_archived and tx_archived > data_arquivamento_fim:
                    continue

            if codigo_imovel:
                tx_imovel = tx.get("codigo_imovel") or ""
                if codigo_imovel.lower() not in tx_imovel.lower():
                    continue
            if codigo_contrato:
                tx_contrato = tx.get("codigo_contrato") or ""
                if codigo_contrato.lower() not in tx_contrato.lower():
                    continue
            if transacao_unique_id:
                tx_unique = tx.get("transacao_unique_id_pipeimob") or ""
                if transacao_unique_id.lower() not in tx_unique.lower():
                    continue

        # Local backend-only filters (always applied)
        if agent:
            tx_agent = tx.get("agente_gestor") or ""
            if agent.lower() not in tx_agent.lower():
                continue
        if category:
            tx_cat = tx.get("categoria_crm") or ""
            if category.lower() not in tx_cat.lower():
                continue
        if financing is not None:
            tx_fin = tx.get("financiamento")
            if tx_fin != financing:
                continue
        if etapa_atual:
            tx_etapa = tx.get("etapa_atual") or ""
            if etapa_atual.lower() not in tx_etapa.lower():
                continue

        filtered.append(tx)
    return filtered

def extract_transaction_date(tx: dict) -> Optional[str]:
    priority_keys = [
        "data_assinatura_ccv",
        "data_ccv",
        "data_assinatura",
        "data_contrato",
        "data_criacao",
        "created_at"
    ]
    for key in priority_keys:
        val = tx.get(key)
        if val is not None and val != "":
            return str(val)
    for k, v in tx.items():
        if isinstance(v, dict):
            for key in priority_keys:
                val = v.get(key)
                if val is not None and val != "":
                    return str(val)
    return None

def parse_date_to_year_month(date_str: str) -> Optional[tuple]:
    if not date_str or not isinstance(date_str, str):
        return None
    import re
    date_str = date_str.strip()
    match_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", date_str)
    if match_iso:
        try:
            year = int(match_iso.group(1))
            month = int(match_iso.group(2))
            if 1 <= month <= 12:
                return year, month
        except ValueError:
            pass
    match_br = re.match(r"^(\d{2})/(\d{2})/(\d{4})", date_str)
    if match_br:
        try:
            month = int(match_br.group(2))
            year = int(match_br.group(3))
            if 1 <= month <= 12:
                return year, month
        except ValueError:
            pass
    return None

def parse_date_to_date_obj(val: Any) -> Optional[date]:
    if val is None:
        return None
    date_str = str(val).strip()
    if date_str == "" or date_str.lower() in ("none", "null"):
        return None

    import re
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    sp_tz = ZoneInfo("America/Sao_Paulo")

    # 1. Try ISO datetime format with time component (e.g. "2026-07-06T01:30:00Z", "2026-07-06T20:30:00-03:00", "2026-07-06 20:30:00")
    iso_datetime_pattern = r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(?:(Z)|([+-]\d{2}):?(\d{2}))?"
    match_dt = re.match(iso_datetime_pattern, date_str)
    if match_dt:
        try:
            year, month, day = int(match_dt.group(1)), int(match_dt.group(2)), int(match_dt.group(3))
            hour, minute = int(match_dt.group(4)), int(match_dt.group(5))
            second = int(match_dt.group(6)) if match_dt.group(6) else 0
            microsecond = int(match_dt.group(7)[:6].ljust(6, '0')) if match_dt.group(7) else 0

            is_z = match_dt.group(8)
            tz_h = match_dt.group(9)
            tz_m = match_dt.group(10)

            if is_z:
                tz = timezone.utc
            elif tz_h is not None:
                offset_sign = -1 if tz_h.startswith('-') else 1
                offset_mins = (abs(int(tz_h)) * 60 + (int(tz_m) if tz_m else 0)) * offset_sign
                tz = timezone(timedelta(minutes=offset_mins))
            else:
                tz = sp_tz

            dt_obj = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=tz)
            dt_sp = dt_obj.astimezone(sp_tz)
            return dt_sp.date()
        except Exception:
            pass

    # 2. Try YYYY-MM-DD (date only)
    match_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_str)
    if match_iso:
        try:
            return date(int(match_iso.group(1)), int(match_iso.group(2)), int(match_iso.group(3)))
        except ValueError:
            pass

    # 3. Try DD/MM/YYYY (date only)
    match_br = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", date_str)
    if match_br:
        try:
            return date(int(match_br.group(3)), int(match_br.group(2)), int(match_br.group(1)))
        except ValueError:
            pass

    return None

def build_timeline_buckets(
    filtered: list,
    start_date: date,
    end_date: date,
    applied_granularity: str
) -> tuple[list[dict], dict]:
    from decimal import Decimal
    import calendar
    from datetime import timedelta

    months_pt_short = {1: "jan", 2: "fev", 3: "mar", 4: "abr", 5: "mai", 6: "jun", 7: "jul", 8: "ago", 9: "set", 10: "out", 11: "nov", 12: "dez"}
    months_pt_title = {1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun", 7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez"}

    buckets = []

    if applied_granularity == "day":
        curr = start_date
        while curr <= end_date:
            dt_str = curr.strftime("%Y-%m-%d")
            buckets.append({
                "bucket_key": dt_str,
                "period_start_date": curr,
                "period_end_date": curr,
                "period_start": dt_str,
                "period_end": dt_str,
                "label": f"{curr.day:02d}/{curr.month:02d}",
                "month": None,
                "count": 0,
                "sales": Decimal("0.0"),
                "commissions": Decimal("0.0")
            })
            curr += timedelta(days=1)

    elif applied_granularity == "week":
        curr_start = start_date
        while curr_start <= end_date:
            days_to_sunday = 6 - curr_start.weekday()
            week_sunday = curr_start + timedelta(days=days_to_sunday)
            curr_end = min(week_sunday, end_date)

            p_start_str = curr_start.strftime("%Y-%m-%d")
            p_end_str = curr_end.strftime("%Y-%m-%d")
            bucket_key = f"{p_start_str}_{p_end_str}"

            if curr_start.year != curr_end.year:
                label = f"{curr_start.day:02d} {months_pt_short[curr_start.month]} {curr_start.year}–{curr_end.day:02d} {months_pt_short[curr_end.month]} {curr_end.year}"
            elif curr_start.month != curr_end.month:
                label = f"{curr_start.day:02d} {months_pt_short[curr_start.month]}–{curr_end.day:02d} {months_pt_short[curr_end.month]}"
            else:
                label = f"{curr_start.day:02d}–{curr_end.day:02d} {months_pt_short[curr_start.month]}"

            buckets.append({
                "bucket_key": bucket_key,
                "period_start_date": curr_start,
                "period_end_date": curr_end,
                "period_start": p_start_str,
                "period_end": p_end_str,
                "label": label,
                "month": None,
                "count": 0,
                "sales": Decimal("0.0"),
                "commissions": Decimal("0.0")
            })
            curr_start = curr_end + timedelta(days=1)

    else:  # "month"
        curr_start = start_date
        while curr_start <= end_date:
            _, last_day = calendar.monthrange(curr_start.year, curr_start.month)
            month_end = date(curr_start.year, curr_start.month, last_day)
            curr_end = min(month_end, end_date)

            p_start_str = curr_start.strftime("%Y-%m-%d")
            p_end_str = curr_end.strftime("%Y-%m-%d")
            month_key = f"{curr_start.year}-{curr_start.month:02d}"

            label = f"{months_pt_title[curr_start.month]}/{str(curr_start.year)[-2:]}"

            buckets.append({
                "bucket_key": month_key,
                "period_start_date": curr_start,
                "period_end_date": curr_end,
                "period_start": p_start_str,
                "period_end": p_end_str,
                "label": label,
                "month": month_key,
                "count": 0,
                "sales": Decimal("0.0"),
                "commissions": Decimal("0.0")
            })
            curr_start = curr_end + timedelta(days=1)

    unclassified = {
        "count": 0,
        "sales": Decimal("0.0"),
        "commissions": Decimal("0.0"),
        "missing_date_count": 0,
        "invalid_date_count": 0,
        "out_of_range_count": 0
    }

    for tx in filtered:
        dt_str = extract_transaction_date(tx)
        tx_date = parse_date_to_date_obj(dt_str) if dt_str else None

        val_sales = Decimal(str(tx.get("valor_contrato") or "0.0"))
        val_comm = Decimal(str(tx.get("total_comissao") or "0.0"))

        if tx_date:
            matched = False
            for b in buckets:
                if b["period_start_date"] <= tx_date <= b["period_end_date"]:
                    b["count"] += 1
                    b["sales"] += val_sales
                    b["commissions"] += val_comm
                    matched = True
                    break
            if not matched:
                unclassified["count"] += 1
                unclassified["sales"] += val_sales
                unclassified["commissions"] += val_comm
                unclassified["out_of_range_count"] += 1
        else:
            unclassified["count"] += 1
            unclassified["sales"] += val_sales
            unclassified["commissions"] += val_comm
            if dt_str is None:
                unclassified["missing_date_count"] += 1
            else:
                unclassified["invalid_date_count"] += 1

    formatted_timeline = []
    for b in buckets:
        if b["sales"] > Decimal("0.0"):
            rate = float(round((b["commissions"] / b["sales"]) * Decimal("100"), 2))
        else:
            rate = 0.0

        formatted_timeline.append({
            "bucket_key": b["bucket_key"],
            "period_start": b["period_start"],
            "period_end": b["period_end"],
            "label": b["label"],
            "month": b["month"],
            "transaction_count": b["count"],
            "total_sales": f"{b['sales']:.2f}",
            "total_commissions": f"{b['commissions']:.2f}",
            "average_commission_rate": rate
        })

    formatted_unclassified = {
        "transaction_count": unclassified["count"],
        "total_sales": f"{unclassified['sales']:.2f}",
        "total_commissions": f"{unclassified['commissions']:.2f}",
        "missing_date_count": unclassified["missing_date_count"],
        "invalid_date_count": unclassified["invalid_date_count"],
        "out_of_range_count": unclassified["out_of_range_count"]
    }

    return formatted_timeline, formatted_unclassified

def compute_dashboard_aggregates(
    filtered: list,
    data_inicio_ccv: Optional[str] = None,
    data_fim_ccv: Optional[str] = None,
    data_inicio_criacao: Optional[str] = None,
    data_fim_criacao: Optional[str] = None,
    requested_granularity: str = "month",
    applied_granularity: str = "month"
) -> dict:
    from decimal import Decimal

    months_pt = {
        "01": "Jan", "02": "Fev", "03": "Mar", "04": "Abr", "05": "Mai", "06": "Jun",
        "07": "Jul", "08": "Ago", "09": "Set", "10": "Out", "11": "Nov", "12": "Dez"
    }

    # 1. Summary
    total_sales = Decimal("0.0")
    total_commissions = Decimal("0.0")
    for tx in filtered:
        total_sales += Decimal(str(tx.get("valor_contrato") or "0.0"))
        total_commissions += Decimal(str(tx.get("total_comissao") or "0.0"))

    avg_rate = float(round((total_commissions / total_sales) * Decimal("100"), 2)) if total_sales > Decimal("0.0") else 0.0

    summary = {
        "total_sales": float(round(total_sales, 2)),
        "total_commissions": float(round(total_commissions, 2)),
        "avg_commission_rate": avg_rate,
        "transaction_count": len(filtered)
    }

    # 2. Origins
    origin_groups = {}
    for tx in filtered:
        origin = tx.get("midia_origem_compradores") or "Não Informado"
        val = Decimal(str(tx.get("valor_contrato") or "0.0"))
        if origin not in origin_groups:
            origin_groups[origin] = {"volume": Decimal("0.0"), "count": 0}
        origin_groups[origin]["volume"] += val
        origin_groups[origin]["count"] += 1
    origins = [
        {"origin": o, "count": stats["count"], "volume": float(round(stats["volume"], 2))}
        for o, stats in origin_groups.items()
    ]
    origins.sort(key=lambda x: x["volume"], reverse=True)

    # 3. Stages
    stage_groups = {}
    for tx in filtered:
        stage = tx.get("etapa_atual") or "Sem Etapa"
        val = Decimal(str(tx.get("valor_contrato") or "0.0"))
        if stage not in stage_groups:
            stage_groups[stage] = {"volume": Decimal("0.0"), "count": 0}
        stage_groups[stage]["volume"] += val
        stage_groups[stage]["count"] += 1
    stages = [
        {"stage": s, "count": stats["count"], "volume": float(round(stats["volume"], 2))}
        for s, stats in stage_groups.items()
    ]
    stages.sort(key=lambda x: x["volume"], reverse=True)

    # 4. Managers
    mgr_groups = {}
    for tx in filtered:
        mgr = tx.get("agente_gestor") or "Sem Gestor"
        val = Decimal(str(tx.get("valor_contrato") or "0.0"))
        if mgr not in mgr_groups:
            mgr_groups[mgr] = {"volume": Decimal("0.0"), "count": 0}
        mgr_groups[mgr]["volume"] += val
        mgr_groups[mgr]["count"] += 1
    managers = []
    for mgr, stats in mgr_groups.items():
        ticket = float(round(stats["volume"] / Decimal(str(stats["count"])), 2)) if stats["count"] > 0 else 0.0
        managers.append({
            "manager": mgr,
            "count": stats["count"],
            "volume": float(round(stats["volume"], 2)),
            "ticket_medio": ticket
        })
    managers.sort(key=lambda x: x["volume"], reverse=True)

    # 5. Payments
    financed_count = 0
    cash_count = 0
    bank_groups = {}
    method_groups = {}
    for tx in filtered:
        is_fin = tx.get("financiamento", False)
        val = Decimal(str(tx.get("valor_contrato") or "0.0"))
        if is_fin:
            financed_count += 1
            bank = tx.get("financiamento_banco") or "Não Informado"
            if bank not in bank_groups:
                bank_groups[bank] = {"volume": Decimal("0.0"), "count": 0}
            bank_groups[bank]["volume"] += val
            bank_groups[bank]["count"] += 1
        else:
            cash_count += 1

        for fp in tx.get("forma_pagamento", []):
            m_name = fp.get("nome") or "Outros"
            m_val = Decimal(str(fp.get("valor") or "0.0"))
            method_groups[m_name] = method_groups.get(m_name, Decimal("0.0")) + m_val

    total_deals = financed_count + cash_count
    ratio = float(round((Decimal(str(financed_count)) / Decimal(str(total_deals))) * Decimal("100"), 2)) if total_deals > 0 else 0.0
    banks = [
        {"bank": b, "count": stats["count"], "volume": float(round(stats["volume"], 2))}
        for b, stats in bank_groups.items()
    ]
    banks.sort(key=lambda x: x["volume"], reverse=True)
    methods = [
        {"method": m, "volume": float(round(v, 2))}
        for m, v in method_groups.items()
    ]
    methods.sort(key=lambda x: x["volume"], reverse=True)
    payments = {
        "financed_count": financed_count,
        "cash_count": cash_count,
        "financing_ratio": ratio,
        "banks": banks,
        "methods": methods
    }

    # 6. Commissions
    commissions = []
    total_comm = Decimal("0.0")
    total_sales_comm = Decimal("0.0")
    for tx in filtered:
        val = Decimal(str(tx.get("valor_contrato") or "0.0"))
        comm = Decimal(str(tx.get("total_comissao") or "0.0"))
        rate = float(round((comm / val) * Decimal("100"), 2)) if val > Decimal("0.0") else 0.0
        total_comm += comm
        total_sales_comm += val
        commissions.append({
            "transaction_id": tx.get("transacao_unique_id_pipeimob") or "",
            "contract_code": tx.get("codigo_contrato") or "",
            "value": float(round(val, 2)),
            "commission": float(round(comm, 2)),
            "rate": rate,
            "manager": tx.get("agente_gestor") or "Sem Gestor"
        })
    avg_rate_comm = float(round((total_comm / total_sales_comm) * Decimal("100"), 2)) if total_sales_comm > Decimal("0.0") else 0.0
    expose_raw = os.getenv("EXPOSE_RAW_TRANSACTIONS", "false").strip().lower() == "true"
    commissions_payload = {
        "total_commissions": float(round(total_comm, 2)),
        "avg_commission_rate": avg_rate_comm,
        "commissions": commissions if expose_raw else []
    }

    # 7. Timeline
    start_str = data_inicio_ccv or data_inicio_criacao
    end_str = data_fim_ccv or data_fim_criacao

    start_date_obj = parse_date_to_date_obj(start_str) if start_str else None
    end_date_obj = parse_date_to_date_obj(end_str) if end_str else None

    if not start_date_obj or not end_date_obj:
        dataset_dates = []
        for tx in filtered:
            dt_str = extract_transaction_date(tx)
            d = parse_date_to_date_obj(dt_str)
            if d:
                dataset_dates.append(d)
        if dataset_dates:
            if not start_date_obj:
                start_date_obj = min(dataset_dates)
            if not end_date_obj:
                end_date_obj = max(dataset_dates)
        else:
            today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
            if not start_date_obj:
                start_date_obj = today.replace(day=1)
            if not end_date_obj:
                end_date_obj = today

    if start_date_obj > end_date_obj:
        start_date_obj, end_date_obj = end_date_obj, start_date_obj

    timeline, unclassified_payload = build_timeline_buckets(
        filtered=filtered,
        start_date=start_date_obj,
        end_date=end_date_obj,
        applied_granularity=applied_granularity
    )

    timeline_count_sum = sum(t["transaction_count"] for t in timeline)
    timeline_sales_sum = sum(Decimal(t["total_sales"]) for t in timeline)
    timeline_comm_sum = sum(Decimal(t["total_commissions"]) for t in timeline)

    unclassified_count = unclassified_payload["transaction_count"]
    unclassified_sales = Decimal(unclassified_payload["total_sales"])
    unclassified_comm = Decimal(unclassified_payload["total_commissions"])

    reconciled_count = (timeline_count_sum + unclassified_count) == summary["transaction_count"]
    reconciled_sales = (timeline_sales_sum + unclassified_sales) == total_sales
    reconciled_comm = (timeline_comm_sum + unclassified_comm) == total_commissions
    is_reconciled = reconciled_count and reconciled_sales and reconciled_comm

    reconciliation = {
        "summary_transaction_count": summary["transaction_count"],
        "timeline_transaction_count": timeline_count_sum,
        "unclassified_transaction_count": unclassified_count,
        "is_reconciled": is_reconciled
    }

    unclassified_payload = {
        "transaction_count": unclassified_count,
        "total_sales": f"{unclassified_sales:.2f}",
        "total_commissions": f"{unclassified_comm:.2f}",
        "missing_date_count": unclassified_payload["missing_date_count"],
        "invalid_date_count": unclassified_payload["invalid_date_count"],
        "out_of_range_count": unclassified_payload["out_of_range_count"]
    }

    months_range = []
    c_y, c_m = start_date_obj.year, start_date_obj.month
    end_y, end_m = end_date_obj.year, end_date_obj.month
    while (c_y, c_m) <= (end_y, end_m):
        months_range.append((c_y, c_m))
        if c_m == 12:
            c_m = 1
            c_y += 1
        else:
            c_m += 1

    # === VGC Commission Financials Analysis ===
    sp_tz = ZoneInfo("America/Sao_Paulo")
    as_of_datetime = datetime.now(sp_tz)
    as_of_date_obj = as_of_datetime.date()
    as_of_date_str = as_of_date_obj.strftime("%Y-%m-%d")

    # Initialize totals
    tot_vgc_total = Decimal("0.0")
    tot_gralha = Decimal("0.0")
    tot_demais = Decimal("0.0")
    tot_unclassified = Decimal("0.0")

    # Source counters
    receipt_date_sources = {
        "data_recebimento_comissao": 0,
        "data_pagamento_comissao": 0,
        "data_pagamento_comissao_prevista": 0,
        "missing": 0
    }

    # Data Quality counters
    valid_split_count = 0
    valid_zero_company_share_count = 0
    missing_array_count = 0
    malformed_array_count = 0
    invalid_item_value_count = 0
    reconciliation_mismatch_count = 0
    reconciliation_diff_sum = Decimal("0")

    # Classification sums by state
    received_total = Decimal("0.0")
    received_gralha = Decimal("0.0")
    received_demais = Decimal("0.0")
    received_unclassified = Decimal("0.0")
    received_count = 0

    pending_total = Decimal("0.0")
    pending_gralha = Decimal("0.0")
    pending_demais = Decimal("0.0")
    pending_unclassified = Decimal("0.0")
    pending_count = 0

    unknown_total = Decimal("0.0")
    unknown_gralha = Decimal("0.0")
    unknown_demais = Decimal("0.0")
    unknown_unclassified = Decimal("0.0")
    unknown_count = 0

    received_date_count = 0
    missing_date_count = 0
    invalid_date_count = 0
    future_date_count = 0

    for tx in filtered:
        # 1. Total Commission
        total_comm_raw = tx.get("total_comissao")
        if total_comm_raw is None or str(total_comm_raw).strip() == "":
            vgc_total = Decimal("0")
        else:
            try:
                vgc_total = to_decimal(total_comm_raw)
            except Exception:
                vgc_total = Decimal("0")

        # 2. Extract split
        ext = extract_commission_split(tx)

        # Track data quality
        if ext.status == "valid":
            if ext.matching_company_items_count > 0:
                valid_split_count += 1
            else:
                valid_zero_company_share_count += 1
        elif ext.status == "missing_array":
            missing_array_count += 1
        elif ext.status == "malformed_array":
            malformed_array_count += 1
        elif ext.status == "invalid_value":
            invalid_item_value_count += 1
        elif ext.status == "reconciliation_mismatch":
            reconciliation_mismatch_count += 1

        # Track reconciliation difference
        if ext.all_participants_amount is not None:
            reconciliation_diff_sum += abs(ext.all_participants_amount - vgc_total)
        else:
            reconciliation_diff_sum += vgc_total

        # 3. Categorize split
        if ext.status == "valid":
            vgc_gralha = ext.gralha_amount
            vgc_demais = vgc_total - vgc_gralha
            vgc_unclassified = Decimal("0")
        else:
            vgc_gralha = Decimal("0")
            vgc_demais = Decimal("0")
            vgc_unclassified = vgc_total

        # 4. Get receipt date and source
        date_str, source = get_receipt_date(tx)
        if source == "data_pagamento_comissao_prevista":
            receipt_date_sources["data_pagamento_comissao_prevista"] += 1
        elif source == "data_recebimento_comissao":
            receipt_date_sources["data_recebimento_comissao"] += 1
        else:
            receipt_date_sources["missing"] += 1

        # 5. Classify by receipt status
        if date_str is None or str(date_str).strip() == "" or str(date_str).strip().lower() in ["none", "null"]:
            status = "missing"
            missing_date_count += 1
        else:
            receipt_date = parse_explicit_date(date_str)
            if receipt_date is None:
                status = "invalid"
                invalid_date_count += 1
            elif receipt_date > as_of_date_obj:
                status = "future"
                future_date_count += 1
            else:
                status = "received"
                received_date_count += 1

        if status == "missing":
            pending_total += vgc_total
            pending_gralha += vgc_gralha
            pending_demais += vgc_demais
            pending_unclassified += vgc_unclassified
            pending_count += 1
        elif status in ("invalid", "future"):
            unknown_total += vgc_total
            unknown_gralha += vgc_gralha
            unknown_demais += vgc_demais
            unknown_unclassified += vgc_unclassified
            unknown_count += 1
        elif status == "received":
            received_total += vgc_total
            received_gralha += vgc_gralha
            received_demais += vgc_demais
            received_unclassified += vgc_unclassified
            received_count += 1

        tot_vgc_total += vgc_total
        tot_gralha += vgc_gralha
        tot_demais += vgc_demais
        tot_unclassified += vgc_unclassified

    # Reconciliations (for validation / dashboard_cache)
    diff1 = abs((tot_gralha + tot_demais + tot_unclassified) - tot_vgc_total)
    diff2 = abs((received_total + pending_total + unknown_total) - tot_vgc_total)
    diff3 = abs((received_gralha + pending_gralha + unknown_gralha) - tot_gralha)
    diff4 = abs((received_demais + pending_demais + unknown_demais) - tot_demais)
    diff5 = abs((received_unclassified + pending_unclassified + unknown_unclassified) - tot_unclassified)

    reconciliation_difference_internal = diff1 + diff2 + diff3 + diff4 + diff5
    reconciled = (
        reconciliation_difference_internal == Decimal("0.0") and
        reconciliation_mismatch_count == 0 and
        invalid_item_value_count == 0 and
        malformed_array_count == 0 and
        missing_array_count == 0
    )

    # Audit quantity reconciliations
    quantity_reconciled = (received_count + pending_count + unknown_count == len(filtered))
    if not quantity_reconciled or tot_gralha < 0 or tot_demais < 0 or tot_unclassified < 0:
        reconciled = False

    received_ratio = float(received_total / tot_vgc_total) if tot_vgc_total > 0 else 0.0
    gralha_ratio = float(tot_gralha / tot_vgc_total) if tot_vgc_total > 0 else 0.0
    demais_ratio = float(tot_demais / tot_vgc_total) if tot_vgc_total > 0 else 0.0
    unclassified_ratio = float(tot_unclassified / tot_vgc_total) if tot_vgc_total > 0 else 0.0

    # 6. Contract build
    has_issues = (
        tot_unclassified > Decimal("0") or
        reconciliation_mismatch_count > 0 or
        invalid_item_value_count > 0 or
        missing_array_count > 0 or
        malformed_array_count > 0
    )
    calculation_status = "partial" if has_issues else "validated"

    vgc_composition = {
        "source_field": "comissionados[].comissionado_valor",
        "company_identification_rule": "comissionado_imobiliária_or_comissionado_filial",
        "calculation_status": calculation_status,
        "total": {
            "amount": f"{tot_vgc_total:.2f}",
            "ratio": 1.0
        },
        "gralha": {
            "amount": f"{tot_gralha:.2f}",
            "ratio": gralha_ratio,
            "received": f"{received_gralha:.2f}",
            "pending": f"{pending_gralha:.2f}",
            "unknown": f"{unknown_gralha:.2f}"
        },
        "demais_participantes": {
            "amount": f"{tot_demais:.2f}",
            "ratio": demais_ratio,
            "received": f"{received_demais:.2f}",
            "pending": f"{pending_demais:.2f}",
            "unknown": f"{unknown_demais:.2f}"
        },
        "corretores_equipe": {
            "amount": f"{tot_demais:.2f}",
            "ratio": demais_ratio,
            "received": f"{received_demais:.2f}",
            "pending": f"{pending_demais:.2f}",
            "unknown": f"{unknown_demais:.2f}"
        },
        "unclassified": {
            "amount": f"{tot_unclassified:.2f}",
            "ratio": unclassified_ratio,
            "received": f"{received_unclassified:.2f}",
            "pending": f"{pending_unclassified:.2f}",
            "unknown": f"{unknown_unclassified:.2f}"
        },
        "data_quality": {
            "records_count": len(filtered),
            "valid_split_count": valid_split_count,
            "valid_zero_company_share_count": valid_zero_company_share_count,
            "missing_array_count": missing_array_count,
            "malformed_array_count": malformed_array_count,
            "invalid_item_value_count": invalid_item_value_count,
            "reconciliation_mismatch_count": reconciliation_mismatch_count,
            "reconciliation_difference": f"{reconciliation_diff_sum:.2f}"
        }
    }

    commission_financials = {
        "period_basis": "ccv",
        "as_of_date": as_of_date_str,
        "timezone": "America/Sao_Paulo",
        "calculation_method": "registered_receipt_date_v1",
        "allocation_method": "status_only",
        "receipt_date_sources": receipt_date_sources,
        "vgc_total": f"{tot_vgc_total:.2f}",
        "composition": {
            "gralha": f"{tot_gralha:.2f}",
            "demais_participantes": f"{tot_demais:.2f}",
            "reconciliation_difference": f"{reconciliation_diff_sum:.2f}",
            "reconciled": reconciled
        },
        "vgc_composition": vgc_composition,
        "received_transactions_count": received_count,
        "pending_transactions_count": pending_count,
        "unknown_transactions_count": unknown_count,
        "receipt_data_quality": {
            "received_date_count": received_date_count,
            "missing_date_count": missing_date_count,
            "invalid_date_count": invalid_date_count,
            "future_date_count": future_date_count
        },
        "received": {
            "total": f"{received_total:.2f}",
            "gralha": f"{received_gralha:.2f}",
            "demais_participantes": f"{received_demais:.2f}",
            "transaction_count": received_count
        },
        "pending": {
            "total": f"{pending_total:.2f}",
            "gralha": f"{pending_gralha:.2f}",
            "demais_participantes": f"{pending_demais:.2f}",
            "transaction_count": pending_count,
            "without_date_count": pending_count
        },
        "unknown": {
            "total": f"{unknown_total:.2f}",
            "gralha": f"{unknown_gralha:.2f}",
            "demais_participantes": f"{unknown_demais:.2f}",
            "transaction_count": unknown_count,
            "invalid_date_count": invalid_date_count,
            "future_date_count": future_date_count
        },
        "received_ratio": received_ratio,
        "semantic_validation": "provisional_v1",
        "disclaimer": (
            "Classificação das datas de recebimento: data válida até a data de referência (as_of_date): recebido; "
            "data ausente: pendente; data futura ou inválida: desconhecido; a classificação é transacional e "
            "não comprova a liquidação de todas as parcelas financeiras."
        )
    }

    # === Sales Cycle (Velocidade de Venda) Analysis ===
    missing_signature_date_count = 0
    missing_capture_date_count = 0
    invalid_date_count = 0
    negative_duration_count = 0

    valid_durations = []
    valid_records = []

    # Timeline initialization: reuse months_range
    sales_cycle_timeline_groups = {}
    for y, m in months_range:
        k_month = f"{y}-{m:02d}"
        parts = k_month.split("-")
        lbl = f"{months_pt.get(parts[1], parts[1])}/{parts[0][-2:]}"
        sales_cycle_timeline_groups[k_month] = {
            "month": k_month,
            "label": lbl,
            "transaction_count": 0,
            "durations": [],
            "within_90_days_count": 0
        }

    for tx in filtered:
        # 1. signature date
        dt_sig_str = extract_transaction_date(tx)
        if dt_sig_str is None or str(dt_sig_str).strip() == "":
            missing_signature_date_count += 1
            continue

        # 2. capture date
        dt_cap_str = tx.get("data_captacao")
        if dt_cap_str is None or str(dt_cap_str).strip() == "":
            missing_capture_date_count += 1
            continue

        # 3. parse dates
        dt_sig = parse_explicit_date(dt_sig_str)
        dt_cap = parse_explicit_date(dt_cap_str)
        if dt_sig is None or dt_cap is None:
            invalid_date_count += 1
            continue

        # 4. negative duration
        if dt_cap > dt_sig:
            negative_duration_count += 1
            continue

        # 5. valid record
        sales_cycle_days = (dt_sig - dt_cap).days
        valid_durations.append(sales_cycle_days)

        # Prepare helper metadata for extremes
        raw_code = tx.get("codigo_imovel")
        raw_title = tx.get("titulo_nome_negocio")

        clean_code = str(raw_code).strip() if raw_code is not None else None
        if clean_code == "":
            clean_code = None

        clean_title = str(raw_title).strip() if raw_title is not None else None
        if clean_title == "":
            clean_title = None

        # Privacy & Security: Sanitization of deal title if it contains sensitive info
        if clean_title is not None:
            import re
            has_sensitive = False
            if re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", clean_title):
                has_sensitive = True
            elif re.search(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", clean_title):
                has_sensitive = True
            elif re.search(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b", clean_title):
                has_sensitive = True
            elif re.search(r"\b(?:\(?\d{2}\)?\s*?)?\d{4,5}-?\d{4}\b", clean_title):
                has_sensitive = True
            if has_sensitive:
                clean_title = None

        valid_records.append({
            "days": sales_cycle_days,
            "dt_sig": dt_sig,
            "code": clean_code,
            "uid": tx.get("transacao_unique_id_pipeimob"),
            "title": clean_title
        })

        # Add to timeline group if matched
        ym_sig = parse_date_to_year_month(dt_sig_str)
        if ym_sig:
            y_s, m_s = ym_sig
            ym_key = f"{y_s}-{m_s:02d}"
            if ym_key in sales_cycle_timeline_groups:
                sales_cycle_timeline_groups[ym_key]["durations"].append(sales_cycle_days)
                if sales_cycle_days <= 90:
                    sales_cycle_timeline_groups[ym_key]["within_90_days_count"] += 1
                sales_cycle_timeline_groups[ym_key]["transaction_count"] += 1

    # Sort durations for percentiles
    valid_durations.sort()
    valid_count = len(valid_durations)
    total_count = len(filtered)

    # Selection of Extremes (Fastest and Longest Sale)
    fastest_sale = None
    longest_sale = None

    if valid_records:
        def make_tiebreaker_key(record, is_longest=False):
            days_key = -record["days"] if is_longest else record["days"]
            dt_sig_key = record["dt_sig"]

            code_none = record["code"] is None
            code_val = record["code"] if not code_none else ""
            code_key = (code_none, code_val)

            uid_none = record["uid"] is None
            uid_val = record["uid"] if not uid_none else ""
            uid_key = (uid_none, uid_val)

            return (days_key, dt_sig_key, code_key, uid_key)

        # 1. Fastest sale
        valid_records.sort(key=lambda r: make_tiebreaker_key(r, is_longest=False))
        fastest_rec = valid_records[0]
        fastest_sale = {
            "days": fastest_rec["days"],
            "property_code": fastest_rec["code"],
            "deal_title": fastest_rec["title"]
        }

        # 2. Longest sale
        valid_records.sort(key=lambda r: make_tiebreaker_key(r, is_longest=True))
        longest_rec = valid_records[0]
        longest_sale = {
            "days": longest_rec["days"],
            "property_code": longest_rec["code"],
            "deal_title": longest_rec["title"]
        }

    # Initialize faixas / buckets
    bucket_counts = {
        "0_30_days": 0,
        "31_60_days": 0,
        "61_90_days": 0,
        "91_180_days": 0,
        "181_365_days": 0,
        "over_365_days": 0
    }

    within_30_days_count = 0
    within_60_days_count = 0
    within_90_days_count = 0

    for days in valid_durations:
        if days <= 30:
            bucket_counts["0_30_days"] += 1
            within_30_days_count += 1
            within_60_days_count += 1
            within_90_days_count += 1
        elif days <= 60:
            bucket_counts["31_60_days"] += 1
            within_60_days_count += 1
            within_90_days_count += 1
        elif days <= 90:
            bucket_counts["61_90_days"] += 1
            within_90_days_count += 1
        elif days <= 180:
            bucket_counts["91_180_days"] += 1
        elif days <= 365:
            bucket_counts["181_365_days"] += 1
        else:
            bucket_counts["over_365_days"] += 1

    # Calculate stats
    if valid_count > 0:
        avg_days = round(sum(valid_durations) / valid_count, 1)
        med_days = calculate_percentile(valid_durations, 0.50)
        p25 = calculate_percentile(valid_durations, 0.25)
        p75 = calculate_percentile(valid_durations, 0.75)
        p90 = calculate_percentile(valid_durations, 0.90)
        min_days = valid_durations[0]
        max_days = valid_durations[-1]
        w90_ratio = round(within_90_days_count / valid_count, 4)
    else:
        avg_days = 0.0
        med_days = 0.0
        p25 = 0.0
        p75 = 0.0
        p90 = 0.0
        min_days = 0
        max_days = 0
        w90_ratio = 0.0

    # Buckets output construction
    buckets_list = [
        {"key": "0_30_days", "label": "Até 30 dias", "min_days": 0, "max_days": 30, "count": bucket_counts["0_30_days"], "ratio": round(bucket_counts["0_30_days"] / valid_count, 4) if valid_count > 0 else 0.0},
        {"key": "31_60_days", "label": "31 a 60 dias", "min_days": 31, "max_days": 60, "count": bucket_counts["31_60_days"], "ratio": round(bucket_counts["31_60_days"] / valid_count, 4) if valid_count > 0 else 0.0},
        {"key": "61_90_days", "label": "61 a 90 dias", "min_days": 61, "max_days": 90, "count": bucket_counts["61_90_days"], "ratio": round(bucket_counts["61_90_days"] / valid_count, 4) if valid_count > 0 else 0.0},
        {"key": "91_180_days", "label": "3 a 6 meses", "min_days": 91, "max_days": 180, "count": bucket_counts["91_180_days"], "ratio": round(bucket_counts["91_180_days"] / valid_count, 4) if valid_count > 0 else 0.0},
        {"key": "181_365_days", "label": "6 a 12 meses", "min_days": 181, "max_days": 365, "count": bucket_counts["181_365_days"], "ratio": round(bucket_counts["181_365_days"] / valid_count, 4) if valid_count > 0 else 0.0},
        {"key": "over_365_days", "label": "Mais de 12 meses", "min_days": 366, "max_days": None, "count": bucket_counts["over_365_days"], "ratio": round(bucket_counts["over_365_days"] / valid_count, 4) if valid_count > 0 else 0.0}
    ]

    # Timeline output construction
    timeline_list = []
    for k_ym in sorted(sales_cycle_timeline_groups.keys()):
        g = sales_cycle_timeline_groups[k_ym]
        durs = sorted(g["durations"])
        cnt = g["transaction_count"]

        t_avg = round(sum(durs) / cnt, 1) if cnt > 0 else 0.0
        t_med = calculate_percentile(durs, 0.50) if cnt > 0 else 0.0
        t_p75 = calculate_percentile(durs, 0.75) if cnt > 0 else 0.0
        t_w90_count = g["within_90_days_count"]
        t_w90_ratio = round(t_w90_count / cnt, 4) if cnt > 0 else 0.0

        timeline_list.append({
            "month": k_ym,
            "label": g["label"],
            "transaction_count": cnt,
            "average_days": t_avg,
            "median_days": t_med,
            "p75_days": t_p75,
            "within_90_days_count": t_w90_count,
            "within_90_days_ratio": t_w90_ratio
        })

    # Reconciliations assertions (Quantity audit)
    buckets_sum = sum(b["count"] for b in buckets_list)
    reconciled_valid = (buckets_sum == valid_count)
    reconciled_total_count = (
        valid_count +
        missing_signature_date_count +
        missing_capture_date_count +
        invalid_date_count +
        negative_duration_count
    ) == total_count
    reconciled_w90 = (
        within_90_days_count ==
        bucket_counts["0_30_days"] +
        bucket_counts["31_60_days"] +
        bucket_counts["61_90_days"]
    )

    sales_cycle_reconciled = (reconciled_valid and reconciled_total_count and reconciled_w90)

    sales_cycle = {
        "period_basis": "ccv",
        "start_field": "data_captacao",
        "end_field": "data_assinatura_ccv",
        "calculation_unit": "days",
        "transaction_count": total_count,
        "valid_transaction_count": valid_count,
        "excluded": {
            "missing_capture_date_count": missing_capture_date_count,
            "missing_signature_date_count": missing_signature_date_count,
            "invalid_date_count": invalid_date_count,
            "negative_duration_count": negative_duration_count
        },
        "average_days": avg_days,
        "median_days": med_days,
        "p25_days": p25,
        "p75_days": p75,
        "p90_days": p90,
        "minimum_days": min_days,
        "maximum_days": max_days,
        "within_30_days_count": within_30_days_count,
        "within_60_days_count": within_60_days_count,
        "within_90_days_count": within_90_days_count,
        "within_90_days_ratio": w90_ratio,
        "buckets": buckets_list,
        "timeline": timeline_list,
        "fastest_sale": fastest_sale,
        "longest_sale": longest_sale
    }

    # Secure diagnostic logging for sales_cycle
    sales_cycle_log = {
        "event": "sales_cycle_analysis_completed",
        "total_transaction_count": total_count,
        "valid_sales_cycle_count": valid_count,
        "missing_capture_date_count": missing_capture_date_count,
        "missing_signature_date_count": missing_signature_date_count,
        "invalid_date_count": invalid_date_count,
        "negative_duration_count": negative_duration_count,
        "within_90_days_count": within_90_days_count,
        "bucket_counts": bucket_counts,
        "reconciled": sales_cycle_reconciled
    }
    print(f"SECURE_LOG: {json.dumps(sales_cycle_log)}")

    # Secure diagnostic logging
    vgc_log = {
        "event": "vgc_analysis_completed_v1",
        "receipt_date_source_data_recebimento_count": receipt_date_sources["data_recebimento_comissao"],
        "receipt_date_source_data_pagamento_count": receipt_date_sources["data_pagamento_comissao"],
        "receipt_date_source_data_pagamento_prevista_count": receipt_date_sources["data_pagamento_comissao_prevista"],
        "received_count": received_count,
        "pending_future_date_count": 0,
        "pending_without_date_count": pending_count,
        "unknown_invalid_date_count": unknown_count
    }
    print(f"SECURE_LOG: {json.dumps(vgc_log)}")

    # === Data Quality (Qualidade dos Dados) Analysis ===
    import collections
    config_status, config_configured, group_mapping, config_official_teams = parse_official_team_groups()

    distinct_agents = {} # key -> {"name": display_name, "branch": display_branch, "tx_count": 0, "teams": set(), "groups_seen": set()}
    unassigned_manager_transactions_count = 0

    tx_evals = []

    for tx in filtered:
        tx_id = tx.get("transacao_unique_id_pipeimob")
        manager_name = tx.get("agente_gestor")

        # Check source field rules
        groups = tx.get("agente_gestor_grupos_a_que_pertence")

        if groups is None:
            legacy_vals = []
            for lf in ["agente_gestor_grupos_a_que_pertence1", "agente_gestor_grupos_a_que_pertence2", "agente_gestor_grupos_a_que_pertence3"]:
                val = tx.get(lf)
                if val is not None and str(val).strip() != "":
                    legacy_vals.append(str(val).strip())
            groups = []
            for name in legacy_vals:
                matched_id = None
                for gid, info in group_mapping.items():
                    if info["name"].lower() == name.lower():
                        matched_id = gid
                        break
                if matched_id:
                    groups.append(matched_id)
                else:
                    groups.append(name)

        if not manager_name or manager_name.strip() == "":
            unassigned_manager_transactions_count += 1
            tx_evals.append({
                "tx_id": tx_id,
                "agent_key": None,
                "confirmed": {"missing_manager_assignment"},
                "review": set(),
                "groups": groups,
                "branch": None
            })
            continue

        manager_name = manager_name.strip()
        branch_val = tx.get("agente_gestor_grupo_filial")
        normalized_name = " ".join(manager_name.split()).lower()
        normalized_branch = " ".join(branch_val.strip().split()).lower() if branch_val else ""
        agent_key = normalized_name + "__" + normalized_branch

        if agent_key not in distinct_agents:
            distinct_agents[agent_key] = {
                "name": manager_name,
                "branch": branch_val,
                "tx_count": 0,
                "teams": set(),
                "groups_seen": set()
            }

        distinct_agents[agent_key]["tx_count"] += 1

        tx_evals.append({
            "tx_id": tx_id,
            "agent_key": agent_key,
            "confirmed": set(),
            "review": set(),
            "groups": groups,
            "branch": branch_val
        })

    for eval_item in tx_evals:
        if "missing_manager_assignment" in eval_item["confirmed"]:
            continue

        groups = eval_item["groups"]
        agent_key = eval_item["agent_key"]

        if config_status == "configured":
            if not groups:
                eval_item["confirmed"].add("missing_team_assignment")
            else:
                mapped = [group_mapping[gid] for gid in groups if gid in group_mapping]
                unmapped = [gid for gid in groups if gid not in group_mapping]

                has_team = any(g["type"] == "team" for g in mapped)
                has_branch_or_other = any(g["type"] in ["branch", "other"] for g in mapped)

                if not has_team:
                    if has_branch_or_other or not unmapped:
                        eval_item["confirmed"].add("missing_team_assignment")

                if unmapped and not has_team:
                    eval_item["review"].add("configuration_mapping_required")

                for g in mapped:
                    if g["type"] == "team":
                        distinct_agents[agent_key]["teams"].add(g["name"])
                    distinct_agents[agent_key]["groups_seen"].add(g["name"])
                for gid in unmapped:
                    distinct_agents[agent_key]["groups_seen"].add(gid)
        else:
            if not groups:
                eval_item["confirmed"].add("missing_team_assignment")
            else:
                eval_item["review"].add("configuration_mapping_required")
                for gid in groups:
                    distinct_agents[agent_key]["groups_seen"].add(gid)

    for agent_key, info in distinct_agents.items():
        if len(info["teams"]) > 1:
            for eval_item in tx_evals:
                if eval_item["agent_key"] == agent_key:
                    eval_item["review"].add("inconsistent_team_assignment")

    affected_transactions_count = 0
    review_only_transactions_count = 0
    compliant_transactions_count = 0

    agent_status_map = {}
    agent_confirmed_issues = collections.defaultdict(set)
    agent_review_issues = collections.defaultdict(set)
    agent_affected_tx_count = collections.defaultdict(int)

    for eval_item in tx_evals:
        agent_key = eval_item["agent_key"]

        if eval_item["confirmed"]:
            affected_transactions_count += 1
        elif eval_item["review"]:
            review_only_transactions_count += 1
        else:
            compliant_transactions_count += 1

        if agent_key:
            agent_confirmed_issues[agent_key].update(eval_item["confirmed"])
            agent_review_issues[agent_key].update(eval_item["review"])
            if eval_item["confirmed"] or eval_item["review"]:
                agent_affected_tx_count[agent_key] += 1

    affected_agents_count = 0
    review_only_agents_count = 0
    compliant_agents_count = 0

    for agent_key in distinct_agents.keys():
        confirmed = agent_confirmed_issues[agent_key]
        review = agent_review_issues[agent_key]

        if confirmed:
            agent_status_map[agent_key] = "affected"
            affected_agents_count += 1
        elif review:
            agent_status_map[agent_key] = "review_only"
            review_only_agents_count += 1
        else:
            agent_status_map[agent_key] = "compliant"
            compliant_agents_count += 1

    issue_template = {
        "missing_team_assignment": {
            "id": "missing_team_assignment",
            "severity": "high",
            "title": "Equipe não vinculada",
            "description": "O agente não possui uma equipe vinculada nos campos de grupos/equipes do Pipeimob.",
            "impact": "As vendas não podem ser classificadas com segurança nos filtros, rankings e comparativos de equipes.",
            "pipeimob_location": "Pipeimob → cadastro do agente ou usuário → grupos/equipes a que pertence.",
            "correction_steps": [
                "Selecione a equipe comercial oficial da pessoa e salve o cadastro. Não utilize filial, cargo, nome da pessoa ou outro grupo administrativo como equipe.",
                "Volte ao BI e clique em Atualizar para revalidar os dados."
            ]
        },
        "configuration_mapping_required": {
            "id": "configuration_mapping_required",
            "severity": "review",
            "title": "Grupo ainda não classificado",
            "description": "O agente possui um grupo no Pipeimob, mas o BI ainda não consegue confirmar se esse grupo representa uma equipe comercial.",
            "impact": "A validação comercial da equipe está pendente até que o ID do grupo seja mapeado.",
            "pipeimob_location": "Configuração do sistema (Render → PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON).",
            "correction_steps": [
                "O agente possui um grupo no Pipeimob, mas o BI ainda não consegue confirmar se esse grupo representa uma equipe comercial. Revise a configuração oficial de equipes."
            ]
        },
        "inconsistent_team_assignment": {
            "id": "inconsistent_team_assignment",
            "severity": "review",
            "title": "Vínculo inconsistente de equipe",
            "description": "O agente está associado a mais de uma equipe oficial diferente durante o período analisado.",
            "impact": "O histórico do agente apresenta conflito de equipes nas transações do período.",
            "pipeimob_location": "Pipeimob → cadastro do agente ou usuário → grupos/equipes a que pertence.",
            "correction_steps": [
                "Revise os negócios do corretor e ajuste o cadastro para garantir o pertencimento a uma única equipe oficial ativa no período."
            ]
        },
        "missing_manager_assignment": {
            "id": "missing_manager_assignment",
            "severity": "high",
            "title": "Gestor ausente",
            "description": "A transação não possui um agente gestor identificado.",
            "impact": "A transação fica sem atribuição a um corretor ou equipe responsável.",
            "pipeimob_location": "Pipeimob → Negócio → Responsável/Gestor.",
            "correction_steps": [
                "A transação não possui agente gestor identificado. Abra o negócio no Pipeimob, vincule o responsável correto e salve."
            ]
        }
    }

    issue_counts = collections.defaultdict(lambda: {"agents": set(), "tx_count": 0})
    for eval_item in tx_evals:
        agent_key = eval_item["agent_key"]
        for issue_id in (eval_item["confirmed"] | eval_item["review"]):
            if agent_key:
                issue_counts[issue_id]["agents"].add(agent_key)
            issue_counts[issue_id]["tx_count"] += 1

    issues_list = []
    for issue_id, counts in issue_counts.items():
        if issue_id in issue_template:
            tpl = issue_template[issue_id].copy()
            tpl["affected_agents_count"] = len(counts["agents"])
            tpl["affected_transactions_count"] = counts["tx_count"]
            issues_list.append(tpl)

    affected_agents_list = []
    review_agents_list = []

    for agent_key, info in distinct_agents.items():
        state = agent_status_map[agent_key]
        confirmed = sorted(list(agent_confirmed_issues[agent_key]))
        review = sorted(list(agent_review_issues[agent_key]))

        detail = {
            "agent_name": info["name"],
            "confirmed_issue_ids": confirmed,
            "review_issue_ids": review,
            "branch_value": info["branch"],
            "affected_transactions_count": agent_affected_tx_count[agent_key]
        }

        resolved_group_names = []
        for gval in info["groups_seen"]:
            if gval in group_mapping:
                resolved_group_names.append(group_mapping[gval]["name"])
            else:
                resolved_group_names.append("Grupo Não Classificado")

        detail["current_team_values"] = sorted(list(set(resolved_group_names)))

        if state == "affected":
            affected_agents_list.append(detail)
        elif state == "review_only":
            review_agents_list.append(detail)

    affected_agents_list.sort(key=lambda x: x["agent_name"])
    review_agents_list.sort(key=lambda x: x["agent_name"])

    distinct_agents_count = len(distinct_agents)

    agent_compliance_ratio = (
        round(compliant_agents_count / distinct_agents_count, 4)
        if distinct_agents_count > 0 else 0.0
    )
    transaction_compliance_ratio = (
        round(compliant_transactions_count / len(filtered), 4)
        if len(filtered) > 0 else 0.0
    )

    if len(filtered) == 0:
        overall_status = "no_data"
    elif affected_agents_count == 0 and review_only_agents_count == 0 and unassigned_manager_transactions_count == 0 and review_only_transactions_count == 0:
        overall_status = "ok"
    else:
        if distinct_agents_count > 0 and (affected_agents_count / distinct_agents_count) > 0.30:
            overall_status = "critical"
        else:
            overall_status = "attention"

    agents_reconciled = (compliant_agents_count + affected_agents_count + review_only_agents_count == distinct_agents_count)
    transactions_reconciled = (compliant_transactions_count + affected_transactions_count + review_only_transactions_count == len(filtered))

    dq_summary = {
        "status": overall_status,
        "distinct_agents_count": distinct_agents_count,
        "compliant_agents_count": compliant_agents_count,
        "affected_agents_count": affected_agents_count,
        "review_only_agents_count": review_only_agents_count,
        "compliant_transactions_count": compliant_transactions_count,
        "affected_transactions_count": affected_transactions_count,
        "review_only_transactions_count": review_only_transactions_count,
        "unassigned_manager_transactions_count": unassigned_manager_transactions_count,
        "agent_compliance_ratio": agent_compliance_ratio,
        "transaction_compliance_ratio": transaction_compliance_ratio
    }

    dq_teams = {
        "source_fields": {
            "primary": "agente_gestor_grupos_a_que_pertence",
            "primary_type": "array_of_group_ids",
            "legacy_text_fields": [
                "agente_gestor_grupos_a_que_pertence1",
                "agente_gestor_grupos_a_que_pertence2",
                "agente_gestor_grupos_a_que_pertence3"
            ]
        },
        "configuration_status": config_status,
        "official_teams_configured": config_configured,
        "official_teams": config_official_teams,
        "issues": issues_list,
        "affected_agents": affected_agents_list,
        "review_agents": review_agents_list,
        "reconciliation": {
            "agents_reconciled": agents_reconciled,
            "transactions_reconciled": transactions_reconciled
        }
    }

    timestamp_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    data_quality = {
        "period_basis": "ccv",
        "generated_at": timestamp_utc,
        "transaction_count": len(filtered),
        "summary": dq_summary,
        "teams": dq_teams
    }

    return {
        "summary": summary,
        "origins": origins,
        "stages": stages,
        "managers": managers,
        "payments": payments,
        "commissions": commissions_payload,
        "timeline": timeline,
        "unclassified": unclassified_payload,
        "reconciliation": reconciliation,
        "commission_financials": commission_financials,
        "sales_cycle": sales_cycle,
        "data_quality": data_quality
    }



def sanitize_transaction(tx: dict) -> dict:
    # 1. compradores count
    compradores_raw = tx.get("compradores")
    if compradores_raw is None:
        # Fallback to checking tx.get("clientes")
        clientes = tx.get("clientes") or []
        compradores_count = sum(1 for c in clientes if isinstance(c, dict) and c.get("papel") == "Comprador")
    else:
        compradores_count = len(compradores_raw) if isinstance(compradores_raw, list) else int(compradores_raw or 0)

    # 2. vendedores count
    vendedores_raw = tx.get("vendedores")
    if vendedores_raw is None:
        # Fallback to checking tx.get("clientes")
        clientes = tx.get("clientes") or []
        vendedores_count = sum(1 for c in clientes if isinstance(c, dict) and c.get("papel") == "Vendedor")
    else:
        vendedores_count = len(vendedores_raw) if isinstance(vendedores_raw, list) else int(vendedores_raw or 0)

    # 3. forma_pagamento summarized (natureza/nome e valor)
    forma_pagamento_raw = tx.get("forma_pagamento") or []
    forma_pagamento_clean = []
    if isinstance(forma_pagamento_raw, list):
        for fp in forma_pagamento_raw:
            if isinstance(fp, dict):
                nome = fp.get("nome") or fp.get("natureza") or "Forma de Pagamento"
                valor = fp.get("valor") or 0.0
                try:
                    fp_val = float(valor)
                except ValueError:
                    fp_val = 0.0
                forma_pagamento_clean.append({"nome": nome, "valor": fp_val})

    # 4. comissionados sanitizados (apenas nome, tipo e valor)
    comissionados_raw = tx.get("comissionados") or []
    comissionados_clean = []
    if isinstance(comissionados_raw, list):
        for c in comissionados_raw:
            if isinstance(c, dict):
                nome = c.get("nome") or c.get("comissionado_nome") or "Comissionado"
                tipo = c.get("tipo") or c.get("participacao") or c.get("papel") or "Corretor"
                valor = c.get("valor") or c.get("comissionado_valor") or 0.0
                is_imob = c.get("comissionado_imobiliária")
                if is_imob is None:
                    is_imob = c.get("comissionado_imobiliaria")
                try:
                    c_val = float(valor)
                except ValueError:
                    c_val = 0.0
                try:
                    c_val_comm = float(c.get("comissionado_valor") or valor)
                except ValueError:
                    c_val_comm = 0.0
                comissionados_clean.append({
                    "nome": nome,
                    "tipo": tipo,
                    "valor": c_val,
                    "comissionado_imobiliaria": bool(is_imob) if is_imob is not None else False,
                    "comissionado_valor": c_val_comm
                })

    # 5. Build sanitized object
    return {
        "transacao_unique_id_pipeimob": tx.get("transacao_unique_id_pipeimob"),
        "codigo_contrato": tx.get("codigo_contrato"),
        "codigo_imovel": tx.get("codigo_imovel"),
        "titulo_nome_negocio": tx.get("titulo_nome_negocio"),
        "data_contrato": tx.get("data_contrato"),
        "data_inicio_venda": tx.get("data_inicio_venda"),
        "data_captacao": tx.get("data_captacao"),
        "data_assinatura_ccv": tx.get("data_assinatura_ccv"),
        "data_ccv": tx.get("data_ccv"),
        "data_assinatura": tx.get("data_assinatura"),
        "data_criacao": tx.get("data_criacao"),
        "created_at": tx.get("created_at"),
        "endereco_logradouro": tx.get("endereco_logradouro"),
        "endereco_numero": tx.get("endereco_numero"),
        "endereco_complemento": tx.get("endereco_complemento"),
        "endereco_bairro": tx.get("endereco_bairro"),
        "endereco_cidade": tx.get("endereco_cidade"),
        "endereco_uf": tx.get("endereco_uf"),
        "endereco_cep": tx.get("endereco_cep"),
        "categoria_crm": tx.get("categoria_crm"),
        "residencial_comercial": tx.get("residencial_comercial"),
        "area_total": tx.get("area_total"),
        "area_util": tx.get("area_util"),
        "qtd_quartos": tx.get("qtd_quartos"),
        "qtd_vagas": tx.get("qtd_vagas"),
        "agente_gestor": tx.get("agente_gestor"),
        "valor_contrato": tx.get("valor_contrato"),
        "total_comissao": tx.get("total_comissao"),
        "comissao_imobiliaria": tx.get("comissao_imobiliaria"),
        "data_recebimento_comissao": tx.get("data_recebimento_comissao"),
        "valor_recebido": tx.get("valor_recebido"),
        "valor_comissao_recebida": tx.get("valor_comissao_recebida"),
        "saldo_comissao": tx.get("saldo_comissao"),
        "status_recebimento": tx.get("status_recebimento"),
        "midia_origem_compradores": tx.get("midia_origem_compradores"),
        "midia_origem_vendedores": tx.get("midia_origem_vendedores"),
        "etapa_atual": tx.get("etapa_atual"),
        "diasemestoque": tx.get("diasemestoque"),
        "financiamento": tx.get("financiamento"),
        "financiamento_banco": tx.get("financiamento_banco"),
        "forma_pagamento": forma_pagamento_clean,
        "compradores": compradores_count,
        "vendedores": vendedores_count,
        "comissionados": comissionados_clean
    }


def process_transactions_exposure(dataset: list) -> list:
    expose_raw = os.getenv("EXPOSE_RAW_TRANSACTIONS", "false").strip().lower() == "true"
    if expose_raw:
        return dataset
    return [sanitize_transaction(tx) for tx in dataset]


# Pydantic Schemas for OpenAPI documentation
class HealthResponse(BaseModel):
    status: str = Field(..., description="Status of the API service", json_schema_extra={"example": "ok"})
    service: str = Field(..., description="Name of the service", json_schema_extra={"example": "pipeimob-report"})
    version: str = Field(..., description="Version of the service", json_schema_extra={"example": "0.1.0"})
    api_version: str = Field(..., description="API version of the service", json_schema_extra={"example": "v2"})
    pipeimob_connection: str = Field(..., description="Connection status to Pipeimob CRM", json_schema_extra={"example": "pending_configuration"})
    data_mode: str = Field(..., description="Active data mode: demo, live, or unconfigured", json_schema_extra={"example": "unconfigured"})
    timestamp: str = Field(..., description="Current timestamp in UTC ISO-8601 format", json_schema_extra={"example": "2026-07-15T12:00:00Z"})

class ResourceCatalog(BaseModel):
    id: str = Field(..., description="Unique resource ID", json_schema_extra={"example": "transactions"})
    name: str = Field(..., description="Resource name", json_schema_extra={"example": "Transações"})
    backend_endpoint: str = Field(..., description="Local backend endpoint for the resource", json_schema_extra={"example": "/api/transactions"})
    pipeimob_endpoint: Optional[str] = Field(None, description="Confirmed Pipeimob endpoint (null if unconfirmed or divergent)", json_schema_extra={"example": "/api/v2/negocios/transacoes"})
    status: str = Field(..., description="Status of the resource integration", json_schema_extra={"example": "implemented_pending_live_configuration"})
    implemented: bool = Field(..., description="Indicates if the resource integration is fully implemented", json_schema_extra={"example": True})
    validated: bool = Field(..., description="Indicates if the resource integration is validated with live credentials", json_schema_extra={"example": False})
    description: str = Field(..., description="Description of the resource", json_schema_extra={"example": "Transações comerciais do Pipeimob"})
    primary_key: str = Field(..., description="Primary key of the resource records", json_schema_extra={"example": "transacao_unique_id_pipeimob"})
    available_fields: List[str] = Field(..., description="List of available fields for extraction")
    supported_filters: List[str] = Field(..., description="List of supported query filters")
    filters_api_direct: List[str] = Field(..., description="List of filters processed directly at the Pipeimob CRM side")
    filters_local_backend: List[str] = Field(..., description="List of filters applied locally at the backend after fetch")
    pagination_parameters: List[str] = Field(default_factory=list, description="List of pagination parameters accepted by the API")
    pending_items: List[str] = Field(..., description="List of pending implementation items")

class CatalogResponse(BaseModel):
    api_version: str = Field(..., description="API version of the service", json_schema_extra={"example": "v2"})
    resources: List[ResourceCatalog] = Field(..., description="List of supported resources in the catalog")

class IntegrationUnavailableResponse(BaseModel):
    detail: str = Field(..., description="Error message detail", json_schema_extra={"example": "Configuration pending. Please set PIPEIMOB_DATA_MODE environment variable."})
    error_code: str = Field(..., description="Standardized error code classification", json_schema_extra={"example": "integration_unconfigured"})
    data_mode: str = Field(..., description="Active data mode", json_schema_extra={"example": "unconfigured"})
    pipeimob_connection: str = Field(..., description="Active connection status", json_schema_extra={"example": "pending_configuration"})

RESPONSES_503 = {
    503: {
        "model": IntegrationUnavailableResponse,
        "description": "503 — Integração Pipeimob não configurada ou temporariamente indisponível.",
        "content": {
            "application/json": {
                "examples": {
                    "integration_unconfigured": {
                        "summary": "Integração não configurada — produção",
                        "value": {
                            "detail": "Configuration pending. Please set PIPEIMOB_DATA_MODE environment variable.",
                            "error_code": "integration_unconfigured",
                            "data_mode": "unconfigured",
                            "pipeimob_connection": "pending_configuration"
                        }
                    },
                    "missing_credentials": {
                        "summary": "Modo live sem credenciais",
                        "value": {
                            "detail": "Pipeimob credentials are not configured on the server.",
                            "error_code": "missing_credentials",
                            "data_mode": "live",
                            "pipeimob_connection": "missing_credentials"
                        }
                    },
                    "authentication_failed": {
                        "summary": "Autenticação falhou",
                        "value": {
                            "detail": "Failed to authenticate with Pipeimob CRM API. Check credentials.",
                            "error_code": "authentication_failed",
                            "data_mode": "live",
                            "pipeimob_connection": "authentication_failed"
                        }
                    },
                    "pipeimob_unavailable": {
                        "summary": "Integração temporariamente indisponível",
                        "value": {
                            "detail": "Pipeimob API is temporarily unavailable.",
                            "error_code": "pipeimob_unavailable",
                            "data_mode": "live",
                            "pipeimob_connection": "unavailable"
                        }
                    },
                    "pipeimob_timeout": {
                        "summary": "Timeout de requisição",
                        "value": {
                            "detail": "Pipeimob CRM API request timed out.",
                            "error_code": "pipeimob_timeout",
                            "data_mode": "live",
                            "pipeimob_connection": "unavailable"
                        }
                    },
                    "invalid_pipeimob_response": {
                        "summary": "Resposta inválida ou vazia",
                        "value": {
                            "detail": "Pipeimob CRM API returned empty transactions dataset.",
                            "error_code": "invalid_pipeimob_response",
                            "data_mode": "live",
                            "pipeimob_connection": "unavailable"
                        }
                    }
                }
            }
        }
    }
}

class AuthErrorResponse(BaseModel):
    detail: str = Field(..., description="Error message detail", json_schema_extra={"example": "Authentication required."})
    error_code: str = Field(..., description="Standardized error code classification", json_schema_extra={"example": "authentication_required"})

RESPONSES_AUTH = {
    401: {
        "model": AuthErrorResponse,
        "description": "401 — Autenticação necessária ou token inválido/expirado.",
        "content": {
            "application/json": {
                "examples": {
                    "authentication_required": {
                        "summary": "Token de autenticação ausente",
                        "value": {
                            "detail": "Authentication required.",
                            "error_code": "authentication_required"
                        }
                    },
                    "invalid_access_token": {
                        "summary": "Token inválido ou expirado",
                        "value": {
                            "detail": "Invalid or expired access token.",
                            "error_code": "invalid_access_token"
                        }
                    }
                }
            }
        }
    },
    403: {
        "model": AuthErrorResponse,
        "description": "403 — Usuário não autorizado.",
        "content": {
            "application/json": {
                "examples": {
                    "forbidden": {
                        "summary": "Permissão negada (domínio/e-mail fora da allowlist)",
                        "value": {
                            "detail": "User is not authorized to access this resource.",
                            "error_code": "forbidden"
                        }
                    }
                }
            }
        }
    }
}

class SanitizedPaymentMethod(BaseModel):
    nome: Optional[str] = Field(None, description="Nome ou natureza da forma de pagamento", json_schema_extra={"example": "Sinal"})
    valor: Optional[float] = Field(None, description="Valor pago", json_schema_extra={"example": 50000.0})

class SanitizedCommissioned(BaseModel):
    nome: Optional[str] = Field(None, description="Nome do comissionado", json_schema_extra={"example": "Corretor X"})
    tipo: Optional[str] = Field(None, description="Tipo de participação ou papel", json_schema_extra={"example": "Captador"})
    valor: Optional[float] = Field(None, description="Valor da comissão em R$", json_schema_extra={"example": 4000.0})

class SanitizedTransaction(BaseModel):
    transacao_unique_id_pipeimob: Optional[str] = Field(None, description="ID único do Pipeimob")
    codigo_contrato: Optional[str] = Field(None, description="Código do contrato")
    codigo_imovel: Optional[str] = Field(None, description="Código do imóvel")
    data_contrato: Optional[str] = Field(None, description="Data do contrato")
    data_inicio_venda: Optional[str] = Field(None, description="Data de início da venda")
    endereco_bairro: Optional[str] = Field(None, description="Bairro")
    endereco_cidade: Optional[str] = Field(None, description="Cidade")
    endereco_uf: Optional[str] = Field(None, description="UF")
    categoria_crm: Optional[str] = Field(None, description="Categoria do imóvel")
    residencial_comercial: Optional[str] = Field(None, description="Finalidade residencial ou comercial")
    area_total: Optional[float] = Field(None, description="Área total")
    area_util: Optional[float] = Field(None, description="Área útil")
    qtd_quartos: Optional[int] = Field(None, description="Quantidade de quartos")
    qtd_vagas: Optional[int] = Field(None, description="Quantidade de vagas de garagem")
    agente_gestor: Optional[str] = Field(None, description="Agente gestor da transação")
    valor_contrato: Optional[float] = Field(None, description="Valor do contrato")
    total_comissao: Optional[float] = Field(None, description="Total geral de comissão (VGC)")
    comissao_imobiliaria: Optional[float] = Field(None, description="Fração de comissão destinada à imobiliária")
    midia_origem_compradores: Optional[str] = Field(None, description="Origem da mídia do comprador")
    midia_origem_vendedores: Optional[str] = Field(None, description="Origem da mídia do vendedor")
    etapa_atual: Optional[str] = Field(None, description="Etapa atual da transação no CRM")
    diasemestoque: Optional[int] = Field(None, description="Dias em estoque")
    financiamento: Optional[bool] = Field(None, description="Indica se houve financiamento bancário")
    financiamento_banco: Optional[str] = Field(None, description="Banco financiador")
    forma_pagamento: List[SanitizedPaymentMethod] = Field(default_factory=list, description="Formas de pagamento resumidas")
    compradores: int = Field(0, description="Quantidade de compradores")
    vendedores: int = Field(0, description="Quantidade de vendedores")
    comissionados: List[SanitizedCommissioned] = Field(default_factory=list, description="Lista de comissionados sanitizada")

class TransactionsDataPayload(BaseModel):
    count: int = Field(..., description="Count of returned transactions", json_schema_extra={"example": 60})
    transactions: List[dict] = Field(..., description="List of transaction objects (sanitized by default in production)")

class TransactionsListResponse(BaseModel):
    data_mode: str = Field(..., description="Active data mode: demo, live, or unconfigured", json_schema_extra={"example": "live"})
    source: str = Field(..., description="Source of data", json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., description="ISO-8601 UTC timestamp", json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: TransactionsDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "count": 0,
                            "transactions": []
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "count": 60,
                            "transactions": []
                        }
                    }
                }
            ]
        }
    }

class TransactionDetailResponse(BaseModel):
    data_mode: str = Field(..., description="Active data mode: demo, live, or unconfigured", json_schema_extra={"example": "live"})
    source: str = Field(..., description="Source of data", json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., description="ISO-8601 UTC timestamp", json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: dict = Field(..., description="Detailed transaction object")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {}
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {}
                    }
                }
            ]
        }
    }

class SummaryDataPayload(BaseModel):
    total_sales: float = Field(..., description="Sum of all contract values", json_schema_extra={"example": 323764790.0})
    total_commissions: float = Field(..., description="Sum of all commission values", json_schema_extra={"example": 17409771.0})
    avg_commission_rate: float = Field(..., description="Weighted average commission percentage", json_schema_extra={"example": 5.50})
    transaction_count: int = Field(..., description="Total count of deals/transactions", json_schema_extra={"example": 60})

class DashboardSummaryResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "live"})
    source: str = Field(..., json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: SummaryDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "total_sales": 1000000.0,
                            "total_commissions": 60000.0,
                            "avg_commission_rate": 6.0,
                            "transaction_count": 1
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "total_sales": 145800000.0,
                            "total_commissions": 8715600.0,
                            "avg_commission_rate": 5.98,
                            "transaction_count": 60
                        }
                    }
                }
            ]
        }
    }

class OriginMetric(BaseModel):
    origin: str = Field(..., description="Lead source name", json_schema_extra={"example": "PORTAL ZAP"})
    count: int = Field(..., description="Number of transactions from this origin", json_schema_extra={"example": 15})
    volume: float = Field(..., description="Total sales volume from this origin", json_schema_extra={"example": 12500000.0})

class OriginsDataPayload(BaseModel):
    origins: List[OriginMetric] = Field(...)

class DashboardOriginsResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "live"})
    source: str = Field(..., json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: OriginsDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "origins": []
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "origins": [
                                {
                                    "origin": "Indicação Direta",
                                    "count": 9,
                                    "volume": 24240000.0
                                }
                            ]
                        }
                    }
                }
            ]
        }
    }

class StageMetric(BaseModel):
    stage: str = Field(..., description="Pipeline stage name", json_schema_extra={"example": "Fechamento"})
    count: int = Field(..., description="Number of transactions in this stage", json_schema_extra={"example": 12})
    volume: float = Field(..., description="Total sales volume in this stage", json_schema_extra={"example": 18500000.0})

class StagesDataPayload(BaseModel):
    stages: List[StageMetric] = Field(...)

class DashboardStagesResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "live"})
    source: str = Field(..., json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: StagesDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "stages": []
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "stages": [
                                {
                                    "stage": "Fechamento",
                                    "count": 12,
                                    "volume": 18500000.0
                                }
                            ]
                        }
                    }
                }
            ]
        }
    }

class ManagerMetric(BaseModel):
    manager: str = Field(..., description="Name of the agent/manager", json_schema_extra={"example": "Corretor Alfa"})
    count: int = Field(..., description="Number of deals closed", json_schema_extra={"example": 10})
    volume: float = Field(..., description="Total sales volume closed", json_schema_extra={"example": 15210759.0})
    ticket_medio: float = Field(..., description="Average contract value", json_schema_extra={"example": 1521075.9})

class ManagersDataPayload(BaseModel):
    managers: List[ManagerMetric] = Field(...)

class DashboardManagersResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "live"})
    source: str = Field(..., json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: ManagersDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "managers": []
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "managers": [
                                {
                                    "manager": "Corretor Alfa",
                                    "count": 10,
                                    "volume": 15210759.0,
                                    "ticket_medio": 1521075.9
                                }
                            ]
                        }
                    }
                }
            ]
        }
    }

class BankMetric(BaseModel):
    bank: str = Field(..., description="Name of financing bank", json_schema_extra={"example": "Instituição A"})
    count: int = Field(..., description="Number of financed deals", json_schema_extra={"example": 15})
    volume: float = Field(..., description="Total contract value volume", json_schema_extra={"example": 18200000.0})

class PaymentMethodMetric(BaseModel):
    method: str = Field(..., description="Payment method name", json_schema_extra={"example": "Sinal"})
    volume: float = Field(..., description="Total volume allocated", json_schema_extra={"example": 6500000.0})

class PaymentsDataPayload(BaseModel):
    financed_count: int = Field(..., description="Number of financed transactions", json_schema_extra={"example": 40})
    cash_count: int = Field(..., description="Number of cash/direct transactions", json_schema_extra={"example": 20})
    financing_ratio: float = Field(..., description="Percentage of deals financed", json_schema_extra={"example": 66.67})
    banks: List[BankMetric] = Field(..., description="Financing banks distribution")
    methods: List[PaymentMethodMetric] = Field(..., description="Payment methods distribution")

class DashboardPaymentsResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "live"})
    source: str = Field(..., json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: PaymentsDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "financed_count": 0,
                            "cash_count": 0,
                            "financing_ratio": 0.0,
                            "banks": [],
                            "methods": []
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "financed_count": 40,
                            "cash_count": 20,
                            "financing_ratio": 66.67,
                            "banks": [],
                            "methods": []
                        }
                    }
                }
            ]
        }
    }

class CommissionMetric(BaseModel):
    transaction_id: str = Field(..., description="Transaction unique ID", json_schema_extra={"example": "tx_demo_101"})
    contract_code: str = Field(..., description="Contract code / ID Negócio", json_schema_extra={"example": "CONTRATO-DEMO-1001"})
    value: float = Field(..., description="Contract value", json_schema_extra={"example": 1750000.0})
    commission: float = Field(..., description="Total commission value", json_schema_extra={"example": 105000.0})
    rate: float = Field(..., description="Commission rate percentage", json_schema_extra={"example": 6.0})
    manager: str = Field(..., description="Agent manager", json_schema_extra={"example": "Corretor Alfa"})

class CommissionsDataPayload(BaseModel):
    total_commissions: float = Field(..., description="Sum of all commissions", json_schema_extra={"example": 17409771.0})
    avg_commission_rate: float = Field(..., description="Overall average commission rate percentage", json_schema_extra={"example": 5.50})
    commissions: List[CommissionMetric] = Field(..., description="List of individual commission rates")

class DashboardCommissionsResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "live"})
    source: str = Field(..., json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: CommissionsDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "total_commissions": 0.0,
                            "avg_commission_rate": 0.0,
                            "commissions": []
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "total_commissions": 17409771.0,
                            "avg_commission_rate": 5.50,
                            "commissions": []
                        }
                    }
                }
            ]
        }
    }

class DashboardTimelineMetric(BaseModel):
    bucket_key: str = Field(..., description="Unique key for the bucket (e.g. 2026-07-01, 2026-07-06_2026-07-12, 2026-07)")
    period_start: str = Field(..., description="Start date of the bucket period (YYYY-MM-DD)")
    period_end: str = Field(..., description="End date of the bucket period (YYYY-MM-DD)")
    label: str = Field(..., description="Display label for the bucket (e.g. 06–12 jul, 01/07, Jul/26)")
    month: Optional[str] = Field(None, description="Month key (YYYY-MM) for month granularity, None for day/week")
    transaction_count: int = Field(..., description="Number of transactions during the bucket period")
    total_sales: str = Field(..., description="Total sales volume as string")
    total_commissions: str = Field(..., description="Total commissions as string")
    average_commission_rate: float = Field(0.0, description="Weighted average commission rate percentage for the bucket")

class TimelineMetric(BaseModel):
    month: str = Field(..., description="Month key (e.g. YYYY-MM)", json_schema_extra={"example": "2026-01"})
    label: str = Field(..., description="Month/Year label (e.g. Jan/26)", json_schema_extra={"example": "Jan/26"})
    transaction_count: int = Field(..., description="Number of transactions during the month", json_schema_extra={"example": 12})
    total_sales: str = Field(..., description="Total sales volume during the month as string", json_schema_extra={"example": "15000000.00"})
    total_commissions: str = Field(..., description="Total commissions during the month as string", json_schema_extra={"example": "50000.00"})

class UnclassifiedTimeline(BaseModel):
    transaction_count: int = Field(..., description="Number of unclassified transactions")
    total_sales: str = Field(..., description="Total sales volume of unclassified transactions as string")
    total_commissions: str = Field(..., description="Total commissions of unclassified transactions as string")
    missing_date_count: int = Field(..., description="Number of transactions missing date field completely")
    invalid_date_count: int = Field(..., description="Number of transactions with invalid/unparseable date string")
    out_of_range_count: int = Field(0, description="Number of transactions with valid dates but outside requested period")

class TimelineReconciliation(BaseModel):
    summary_transaction_count: int = Field(..., description="Total transactions in summary")
    timeline_transaction_count: int = Field(..., description="Total classified transactions in timeline")
    unclassified_transaction_count: int = Field(..., description="Total unclassified transactions")
    is_reconciled: bool = Field(..., description="Flag indicating if timeline count + unclassified count equals summary count")

class TimelineDataPayload(BaseModel):
    timeline: List[TimelineMetric] = Field(...)
    unclassified: UnclassifiedTimeline = Field(...)
    reconciliation: TimelineReconciliation = Field(...)

class DashboardTimelineResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "live"})
    source: str = Field(..., json_schema_extra={"example": "pipeimob_api_v2"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: TimelineDataPayload = Field(...)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Resposta live — exemplo estrutural",
                    "description": "Resposta retornada em modo de integração real com a API do Pipeimob.",
                    "value": {
                        "data_mode": "live",
                        "source": "pipeimob_api_v2",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "timeline": []
                        }
                    }
                },
                {
                    "summary": "Resposta demo — somente desenvolvimento/testes",
                    "description": "Exemplo demonstrativo utilizado apenas em desenvolvimento ou testes. Não representa dados reais do Pipeimob.",
                    "value": {
                        "data_mode": "demo",
                        "source": "synthetic_mock",
                        "generated_at": "2026-07-15T12:00:00Z",
                        "data": {
                            "timeline": [
                                {
                                    "month": "Jan/26",
                                    "volume": 15000000.0,
                                    "count": 12
                                }
                            ]
                        }
                    }
                }
            ]
        }
    }

class VGCComposition(BaseModel):
    gralha: str
    demais_participantes: str
    reconciliation_difference: str
    reconciled: bool

class VGCReceived(BaseModel):
    total: str
    gralha: str
    demais_participantes: str
    transaction_count: int

class VGCPending(BaseModel):
    total: str
    gralha: str
    demais_participantes: str
    transaction_count: int
    without_date_count: int

class VGCUnknown(BaseModel):
    total: str
    gralha: str
    demais_participantes: str
    transaction_count: int
    invalid_date_count: int
    future_date_count: int

class ReceiptDateSources(BaseModel):
    data_recebimento_comissao: int
    data_pagamento_comissao: int
    data_pagamento_comissao_prevista: Optional[int] = None
    missing: int

class VGCCompositionDetail(BaseModel):
    amount: str
    ratio: float
    received: str
    pending: str
    unknown: str

class VGCCompositionTotal(BaseModel):
    amount: str
    ratio: float = 1.0

class VGCCompositionDataQuality(BaseModel):
    records_count: int
    valid_split_count: int
    valid_zero_company_share_count: int
    missing_array_count: int
    malformed_array_count: int
    invalid_item_value_count: int
    reconciliation_mismatch_count: int
    reconciliation_difference: str

class VGCCompositionV2(BaseModel):
    source_field: str = "comissionados[].comissionado_valor"
    company_identification_rule: str = "comissionado_imobiliária_or_comissionado_filial"
    calculation_status: str = "validated"
    total: VGCCompositionTotal
    gralha: VGCCompositionDetail
    demais_participantes: VGCCompositionDetail
    corretores_equipe: VGCCompositionDetail
    unclassified: VGCCompositionDetail
    data_quality: VGCCompositionDataQuality

class ReceiptDataQuality(BaseModel):
    received_date_count: int
    missing_date_count: int
    invalid_date_count: int
    future_date_count: int

class CommissionFinancials(BaseModel):
    period_basis: str = "ccv"
    as_of_date: str
    timezone: str = "America/Sao_Paulo"
    calculation_method: str = "registered_receipt_date_v1"
    allocation_method: str = "status_only"
    receipt_date_sources: ReceiptDateSources
    vgc_total: str
    composition: Optional[VGCComposition] = None
    vgc_composition: VGCCompositionV2
    received: VGCReceived
    pending: VGCPending
    unknown: VGCUnknown
    received_ratio: float
    semantic_validation: str = "provisional_v1"
    disclaimer: Optional[str] = None
    received_transactions_count: int
    pending_transactions_count: int
    unknown_transactions_count: int
    receipt_data_quality: ReceiptDataQuality

class SalesCycleBucket(BaseModel):
    key: str
    label: str
    min_days: int
    max_days: Optional[int] = None
    count: int
    ratio: float

class SalesCycleTimelineItem(BaseModel):
    month: str
    label: str
    transaction_count: int
    average_days: float
    median_days: float
    p75_days: float
    within_90_days_count: int
    within_90_days_ratio: float

class SalesCycleExcluded(BaseModel):
    missing_capture_date_count: int
    missing_signature_date_count: int
    invalid_date_count: int
    negative_duration_count: int

class SalesCycleExtreme(BaseModel):
    days: int
    property_code: Optional[str] = None
    deal_title: Optional[str] = None

class SalesCyclePayload(BaseModel):
    period_basis: str = "ccv"
    start_field: str = "data_captacao"
    end_field: str = "data_assinatura_ccv"
    calculation_unit: str = "days"
    transaction_count: int
    valid_transaction_count: int
    excluded: SalesCycleExcluded
    average_days: float
    median_days: float
    p25_days: float
    p75_days: float
    p90_days: float
    minimum_days: int
    maximum_days: int
    within_30_days_count: int
    within_60_days_count: int
    within_90_days_count: int
    within_90_days_ratio: float
    buckets: List[SalesCycleBucket]
    timeline: List[SalesCycleTimelineItem]
    fastest_sale: Optional[SalesCycleExtreme] = None
    longest_sale: Optional[SalesCycleExtreme] = None

class DashboardPeriod(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None

class DataQualityIssue(BaseModel):
    id: str
    severity: str
    title: str
    description: str
    affected_agents_count: int
    affected_transactions_count: int
    impact: str
    pipeimob_location: str
    correction_steps: List[str]

class DataQualityAgentDetail(BaseModel):
    agent_name: str
    confirmed_issue_ids: List[str]
    review_issue_ids: List[str]
    current_team_values: List[str]
    branch_value: Optional[str] = None
    affected_transactions_count: int

class DataQualitySummary(BaseModel):
    status: str
    distinct_agents_count: int
    compliant_agents_count: int
    affected_agents_count: int
    review_only_agents_count: int
    compliant_transactions_count: int
    affected_transactions_count: int
    review_only_transactions_count: int
    unassigned_manager_transactions_count: int
    agent_compliance_ratio: float
    transaction_compliance_ratio: float

class DataQualityTeams(BaseModel):
    source_fields: dict
    configuration_status: str
    official_teams_configured: bool
    official_teams: List[str]
    issues: List[DataQualityIssue]
    affected_agents: List[DataQualityAgentDetail]
    review_agents: List[DataQualityAgentDetail]
    reconciliation: dict

class DataQualityPayload(BaseModel):
    period_basis: str
    generated_at: str
    transaction_count: int
    summary: DataQualitySummary
    teams: DataQualityTeams

class DashboardFullResponse(BaseModel):
    data_mode: str
    source: str
    period: DashboardPeriod
    requested_granularity: str = "month"
    applied_granularity: str = "month"
    available_granularities: List[str] = Field(default_factory=lambda: ["day", "week", "month"])
    pages_fetched: int
    transaction_count: int
    summary: SummaryDataPayload
    origins: List[OriginMetric]
    stages: List[StageMetric]
    managers: List[ManagerMetric]
    payments: PaymentsDataPayload
    commissions: CommissionsDataPayload
    timeline: List[DashboardTimelineMetric]
    unclassified: Optional[UnclassifiedTimeline] = None
    reconciliation: Optional[TimelineReconciliation] = None
    sales_cycle: Optional[SalesCyclePayload] = None
    schema_version: Optional[str] = "1.0"
    generated_at: Optional[str] = None
    filters_applied: Optional[dict] = None
    commission_financials: Optional[CommissionFinancials] = None
    debug_metrics: Optional[dict] = None
    data_quality: Optional[DataQualityPayload] = None

# Helper to format and add X-Data-Mode response headers
def get_metadata_wrapper(data_mode: str, source: str):
    timestamp_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "data_mode": data_mode,
        "source": source,
        "generated_at": timestamp_utc
    }
# Endpoint routes
@app.get(
    "/api/health",
    response_model=HealthResponse,
    summary="Get Service Health Status",
    description="Returns HTTP 200 and health info when service is running. Does not perform auth or external network calls."
)
async def get_health():
    timestamp_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    data_mode, conn_status = get_current_data_mode_and_connection()

    return HealthResponse(
        status="ok",
        service="pipeimob-report",
        version="0.1.0",
        api_version="v2",
        pipeimob_connection=conn_status,
        data_mode=data_mode,
        timestamp=timestamp_utc
    )

@app.get(
    "/api/catalog",
    response_model=CatalogResponse,
    summary="Get Resource Catalog",
    description="Returns the integration roadmap status, available fields, filters and pending items for Pipeimob resources."
)
async def get_catalog():
    transactions_resource = ResourceCatalog(
        id="transactions",
        name="Transações",
        backend_endpoint="/api/transactions",
        pipeimob_endpoint="/api/v2/negocios/transacoes",
        status="implemented_pending_live_validation",
        implemented=True,
        validated=False,
        description="Transações comerciais do Pipeimob",
        primary_key="transacao_unique_id_pipeimob",
        available_fields=[
            "transacao_unique_id_pipeimob",
            "codigo_contrato",
            "codigo_imovel",
            "etapa_atual",
            "data_contrato",
            "data_inicio_venda",
            "valor_contrato",
            "total_comissao",
            "comissao_imobiliaria",
            "agente_gestor",
            "midia_origem_compradores",
            "forma_pagamento",
            "comissionados",
            "clientes"
        ],
        supported_filters=[
            "data_inicio_criacao",
            "data_fim_criacao",
            "data_inicio_ccv",
            "data_fim_ccv",
            "data_arquivamento_inicio",
            "data_arquivamento_fim",
            "codigo_imovel",
            "codigo_contrato",
            "transacao_unique_id"
        ],
        filters_api_direct=[
            "data_inicio_criacao",
            "data_fim_criacao",
            "data_inicio_ccv",
            "data_fim_ccv",
            "data_arquivamento_inicio",
            "data_arquivamento_fim",
            "codigo_imovel",
            "codigo_contrato",
            "transacao_unique_id"
        ],
        filters_local_backend=[
            "agent",
            "category",
            "financing",
            "etapa_atual"
        ],
        pagination_parameters=[
            "pagina"
        ],
        pending_items=[
            "etapa_atual é texto livre",
            "agrupamentos por etapa exigem normalização local"
        ]
    )

    return CatalogResponse(
        api_version="v2",
        resources=[transactions_resource]
    )

_jwk_clients = {}

def get_jwk_client(jwks_url: Optional[str] = None):
    """Return one cached JWKS client per explicitly trusted Supabase project."""
    global _jwk_clients
    jwks_url = jwks_url or os.getenv("SUPABASE_JWKS_URL")
    if not jwks_url:
        return None
    if jwks_url not in _jwk_clients:
        from jwt import PyJWKClient
        _jwk_clients[jwks_url] = PyJWKClient(jwks_url)
    return _jwk_clients[jwks_url]


def _configured_asymmetric_auth_projects():
    """Build the exact issuer -> JWKS allowlist used for asymmetric JWTs."""
    projects = []
    primary_issuer = os.getenv("SUPABASE_ISSUER")
    primary_jwks_url = os.getenv("SUPABASE_JWKS_URL")
    secondary_issuer = os.getenv("SUPABASE_SECONDARY_ISSUER")
    secondary_jwks_url = os.getenv("SUPABASE_SECONDARY_JWKS_URL")

    if primary_issuer and primary_jwks_url:
        projects.append((primary_issuer.rstrip("/"), primary_jwks_url))

    if bool(secondary_issuer) != bool(secondary_jwks_url):
        raise HTTPException(
            status_code=500,
            detail=(
                "Server configuration error: SUPABASE_SECONDARY_ISSUER and "
                "SUPABASE_SECONDARY_JWKS_URL must be configured together."
            ),
        )

    if secondary_issuer and secondary_jwks_url:
        normalized_secondary = secondary_issuer.rstrip("/")
        if not any(issuer == normalized_secondary for issuer, _ in projects):
            projects.append((normalized_secondary, secondary_jwks_url))

    return projects


authorization_role_status = "unresolved"


def _log_auth_failure(reason: str, *, alg: Optional[str] = None, error_type: Optional[str] = None) -> None:
    """Registra somente metadados seguros para diagnosticar rejeições JWT."""
    event = {
        "event": "auth_validation_failed",
        "reason": reason,
    }
    if alg:
        event["alg"] = alg
    if error_type:
        event["error_type"] = error_type
    print(f"SECURE_LOG: {json.dumps(event)}")

def get_db_session():
    try:
        from database import SessionLocal
        if not SessionLocal:
            session_factory = None
        else:
            session_factory = SessionLocal
    except Exception:
        session_factory = None

    if session_factory is None:
        yield None
        return

    db = session_factory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@app.exception_handler(DatasetWarmingError)
async def dataset_warming_exception_handler(request: Request, exc: DatasetWarmingError):
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": str(exc.retry_after)},
        content={
            "code": "dataset_warming",
            "error_code": "dataset_warming",
            "retry_after_seconds": exc.retry_after,
            "detail": "O dataset do Pipeimob está sendo carregado no servidor. Por favor, tente novamente em alguns segundos."
        }
    )

class AuthException(Exception):
    def __init__(self, status_code: int, detail: str, error_code: str):
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code

@app.exception_handler(AuthException)
async def auth_exception_handler(request: Request, exc: AuthException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "error_code": exc.error_code
        }
    )

# CORS Configuration
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins: List[str] = []
if allowed_origins_env:
    allowed_origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
else:
    allowed_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://.*--happy-data-hugger\.lovable\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get(
    "/api/version",
    summary="Get Service Build Version Info",
    description="Returns the deployed commit hash and environment."
)
async def get_version():
    commit_hash = os.getenv("RENDER_GIT_COMMIT") or "unknown"
    app_env = os.getenv("APP_ENV", "production").lower()
    res = {
        "commit_hash": commit_hash,
        "app_env": app_env
    }
    if app_env != "production":
        branch = os.getenv("RENDER_GIT_BRANCH")
        if branch:
            res["branch"] = branch
    return res



async def verify_backend_api_key(
    authorization: Optional[str] = Header(None)
):
    import jwt
    import time

    if not authorization:
        _log_auth_failure("authorization_missing")
        raise AuthException(
            status_code=401,
            detail="Authentication required.",
            error_code="authentication_required"
        )

    if not authorization.startswith("Bearer "):
        _log_auth_failure("authorization_scheme_invalid")
        raise AuthException(
            status_code=401,
            detail="Invalid or expired access token.",
            error_code="invalid_access_token"
        )

    token = authorization.split(" ")[1]

    try:
        app_env = os.getenv("APP_ENV", "production").lower()
        jwt_secret = os.getenv("SUPABASE_JWT_SECRET") or ("secret" if app_env not in ["production", "staging"] else None)
        expected_aud = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
        expected_iss = os.getenv("SUPABASE_ISSUER")

        if app_env in ["production", "staging"] and not expected_iss:
            raise HTTPException(
                status_code=500,
                detail="Server configuration error: SUPABASE_ISSUER environment variable is required in production and staging."
            )

        try:
            header = jwt.get_unverified_header(token)
            alg = header.get("alg")
        except Exception as exc:
            _log_auth_failure("token_header_invalid", error_type=type(exc).__name__)
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )

        ALLOWED_ALGORITHMS = ["HS256", "RS256", "ES256"]
        if not alg or alg not in ALLOWED_ALGORITHMS:
            _log_auth_failure("algorithm_not_allowed", alg=alg)
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )

        if alg == "HS256":
            if not jwt_secret:
                _log_auth_failure("hs256_secret_missing", alg=alg)
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )
            try:
                payload = jwt.decode(
                    token,
                    jwt_secret,
                    algorithms=["HS256"],
                    audience=expected_aud,
                    issuer=expected_iss,
                    options={"require": ["exp", "sub"]}
                )
            except AuthException:
                raise
            except Exception as exc:
                _log_auth_failure("hs256_decode_failed", alg=alg, error_type=type(exc).__name__)
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )

        elif alg in ["RS256", "ES256"]:
            # Require 'kid' header for asymmetric tokens
            if not isinstance(header, dict) or "kid" not in header or not header["kid"]:
                _log_auth_failure("asymmetric_kid_missing", alg=alg)
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )

            auth_projects = _configured_asymmetric_auth_projects()
            if not auth_projects:
                _log_auth_failure("jwks_url_missing", alg=alg)
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )

            try:
                unverified_payload = jwt.decode(
                    token,
                    options={
                        "verify_signature": False,
                        "verify_aud": False,
                        "verify_exp": False,
                    },
                )
                token_issuer = str(unverified_payload.get("iss") or "").rstrip("/")
            except Exception as exc:
                _log_auth_failure("token_payload_invalid", alg=alg, error_type=type(exc).__name__)
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )

            selected_project = next(
                (
                    (issuer, project_jwks_url)
                    for issuer, project_jwks_url in auth_projects
                    if issuer == token_issuer
                ),
                None,
            )
            if selected_project is None:
                _log_auth_failure("issuer_not_trusted", alg=alg)
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )

            expected_iss, jwks_url = selected_project

            from jwt.exceptions import PyJWKClientConnectionError
            client = get_jwk_client(jwks_url)
            try:
                jwk_set = client.get_jwk_set()
            except PyJWKClientConnectionError:
                _log_auth_failure("jwks_unavailable", alg=alg)
                raise AuthException(
                    status_code=503,
                    detail="Supabase JWKS service is temporarily unavailable.",
                    error_code="supabase_jwks_unavailable"
                )
            except Exception as exc:
                _log_auth_failure("jwks_invalid", alg=alg, error_type=type(exc).__name__)
                raise AuthException(
                    status_code=503,
                    detail="Supabase JWKS response is malformed or invalid.",
                    error_code="supabase_jwks_invalid"
                )

            if not jwk_set or not hasattr(jwk_set, "keys") or not jwk_set.keys:
                _log_auth_failure("jwks_empty", alg=alg)
                raise AuthException(
                    status_code=503,
                    detail="Supabase JWKS response is empty or invalid.",
                    error_code="supabase_jwks_invalid"
                )


            try:
                signing_key = client.get_signing_key_from_jwt(token)
                payload = jwt.decode(
                    token,
                    signing_key.key,
                    algorithms=[alg],
                    audience=expected_aud,
                    issuer=expected_iss,
                    options={"require": ["exp", "sub"]}
                )
            except AuthException:
                raise
            except Exception as exc:
                _log_auth_failure("asymmetric_decode_failed", alg=alg, error_type=type(exc).__name__)
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )


        # Additional required claims validation (both JWKS & Dev)
        # 1. role must be authenticated
        if payload.get("role") != "authenticated":
            _log_auth_failure("role_invalid", alg=alg)
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )

        # 2. email must be present
        if not payload.get("email") or not isinstance(payload.get("email"), str):
            _log_auth_failure("email_claim_missing", alg=alg)
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )

        # 3. sub must be present
        if not payload.get("sub") or not isinstance(payload.get("sub"), str):
            _log_auth_failure("subject_claim_missing", alg=alg)
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )

        # 4. issuer check if expected
        if expected_iss and payload.get("iss") != expected_iss:
            _log_auth_failure("issuer_mismatch", alg=alg)
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )

        # 5. audience check
        if expected_aud and payload.get("aud") != expected_aud:
            _log_auth_failure("audience_mismatch", alg=alg)
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )

    except AuthException:
        raise
    except HTTPException:
        raise

    except jwt.ExpiredSignatureError:
        raise AuthException(
            status_code=401,
            detail="Invalid or expired access token.",
            error_code="invalid_access_token"
        )
    except jwt.InvalidIssuerError:
        raise AuthException(
            status_code=401,
            detail="Invalid or expired access token.",
            error_code="invalid_access_token"
        )
    except jwt.InvalidAudienceError:
        raise AuthException(
            status_code=401,
            detail="Invalid or expired access token.",
            error_code="invalid_access_token"
        )
    except Exception:
        raise AuthException(
            status_code=401,
            detail="Invalid or expired access token.",
            error_code="invalid_access_token"
        )



    user_email = payload.get("email")
    user_email = user_email.lower().strip()
    allowed_emails_env = os.getenv("ALLOWED_USER_EMAILS", "")
    allowed_domains_env = os.getenv("ALLOWED_EMAIL_DOMAINS", "gralhaimoveis.com.br")

    allowed_emails = [e.strip().lower() for e in allowed_emails_env.split(",") if e.strip()]
    allowed_domains = [d.strip().lower() for d in allowed_domains_env.split(",") if d.strip()]

    email_parts = user_email.split("@")
    user_domain = email_parts[1] if len(email_parts) > 1 else ""

    is_authorized = False
    if user_email in allowed_emails:
        is_authorized = True
    elif user_domain in allowed_domains:
        is_authorized = True

    if not is_authorized:
        raise AuthException(
            status_code=403,
            detail="User is not authorized to access this resource.",
            error_code="forbidden"
        )

    return payload

def get_and_validate_contracts_control_config() -> set:
    writes_enabled_raw = os.getenv("CONTRACTS_CONTROL_WRITES_ENABLED", "false").lower()
    if writes_enabled_raw not in ("true", "false"):
        raise HTTPException(
            status_code=503,
            detail="Invalid configuration: WRITES_ENABLED is not a valid boolean."
        )

    writes_enabled = (writes_enabled_raw == "true")
    if not writes_enabled:
        return set()

    raw_subs = os.getenv("CONTRACTS_CONTROL_ADMIN_SUBS", "")
    if not raw_subs:
        raise HTTPException(
            status_code=503,
            detail="Invalid configuration: ADMIN_SUBS is empty when writes are enabled."
        )

    items = raw_subs.split(",")
    seen = set()
    for item in items:
        if not item:
            raise HTTPException(
                status_code=503,
                detail="Invalid configuration: ADMIN_SUBS contains empty entries."
            )
        if any(c.isspace() for c in item):
            raise HTTPException(
                status_code=503,
                detail="Invalid configuration: ADMIN_SUBS entries cannot contain spaces."
            )
        if item in seen:
            raise HTTPException(
                status_code=503,
                detail="Invalid configuration: ADMIN_SUBS contains duplicate entries."
            )
        seen.add(item)
    return seen

async def require_contracts_control_temporary_admin(
    payload: dict = Depends(verify_backend_api_key)
) -> str:
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=401,
            detail="Authentication required: claim sub is missing."
        )

    admin_subs = get_and_validate_contracts_control_config()

    writes_enabled_raw = os.getenv("CONTRACTS_CONTROL_WRITES_ENABLED", "false").lower()
    if writes_enabled_raw != "true":
        raise HTTPException(
            status_code=403,
            detail="Contracts Control write operations are disabled."
        )

    if sub not in admin_subs:
        raise HTTPException(
            status_code=403,
            detail="Contracts Control write operations are unauthorized."
        )
    return sub

@app.get(
    "/api/transactions",
    response_model=TransactionsListResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="List Transactions",
    description="Returns list of transactions matching the specified query filters. In live mode, period filters (data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim) and direct search filters (codigo_imovel, codigo_contrato, transacao_unique_id) are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_transactions(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    exposed_txs = process_transactions_exposure(filtered)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = TransactionsDataPayload(count=len(exposed_txs), transactions=exposed_txs)
    return meta

@app.get(
    "/api/transactions/{id}",
    response_model=TransactionDetailResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Transaction by ID",
    description="Returns the details of a single transaction by ID (transacao_unique_id_pipeimob or codigo_contrato). In live mode, fetches real transaction from Pipeimob. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_transaction_by_id(
    id: str,
    response: Response,
    request: Request
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    # In live mode we must pass at least one direct filter, so we pass both or try to load by transacao_unique_id or codigo_contrato
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(transacao_unique_id=id, request_id=req_id)
    validate_dataset_origin(mode, src, dataset)

    target_tx = None
    for tx in dataset:
        if tx.get("transacao_unique_id_pipeimob") == id or tx.get("codigo_contrato") == id:
            target_tx = tx
            break

    if not target_tx:
        try:
            mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(codigo_contrato=id, request_id=req_id)
            validate_dataset_origin(mode, src, dataset)
            for tx in dataset:
                if tx.get("transacao_unique_id_pipeimob") == id or tx.get("codigo_contrato") == id:
                    target_tx = tx
                    break
        except Exception:
            pass

    if not target_tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    expose_raw = os.getenv("EXPOSE_RAW_TRANSACTIONS", "false").strip().lower() == "true"
    exposed_tx = target_tx if expose_raw else sanitize_transaction(target_tx)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = exposed_tx
    return meta

@app.get(
    "/api/dashboard/summary",
    response_model=DashboardSummaryResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Dashboard BI Summary metrics",
    description="Computes total sales volume, commissions, weighted avg commission rate, and transaction count. In live mode, period filters and direct search filters are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_summary(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina=None, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    aggregates = compute_dashboard_aggregates(filtered, data_inicio_ccv, data_fim_ccv, data_inicio_criacao, data_fim_criacao)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = SummaryDataPayload(**aggregates["summary"])
    return meta

@app.get(
    "/api/dashboard/origins",
    response_model=DashboardOriginsResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Buyer Origins distribution",
    description="Groups sales volume and transaction count by lead origin source. In live mode, period filters and direct search filters are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_origins(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina=None, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    aggregates = compute_dashboard_aggregates(filtered, data_inicio_ccv, data_fim_ccv, data_inicio_criacao, data_fim_criacao)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = OriginsDataPayload(origins=[OriginMetric(**o) for o in aggregates["origins"]])
    return meta

@app.get(
    "/api/dashboard/stages",
    response_model=DashboardStagesResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Stages distribution",
    description="Groups sales volume and transaction count by CRM pipeline stage. In live mode, period filters and direct search filters are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_stages(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina=None, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    aggregates = compute_dashboard_aggregates(filtered, data_inicio_ccv, data_fim_ccv, data_inicio_criacao, data_fim_criacao)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = StagesDataPayload(stages=[StageMetric(**s) for s in aggregates["stages"]])
    return meta

@app.get(
    "/api/dashboard/managers",
    response_model=DashboardManagersResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Manager Leaderboard",
    description="Computes leaderboard ranking of managers by sales volume, transaction count, and average ticket size. In live mode, period filters and direct search filters are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_managers(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina=None, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    aggregates = compute_dashboard_aggregates(filtered, data_inicio_ccv, data_fim_ccv, data_inicio_criacao, data_fim_criacao)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = ManagersDataPayload(managers=[ManagerMetric(**m) for m in aggregates["managers"]])
    return meta

@app.get(
    "/api/dashboard/payments",
    response_model=DashboardPaymentsResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Payment Methods and Financing distribution",
    description="Aggregates payment direct vs financing ratio, bank distributions, and detailed signals/methods volumes. In live mode, period filters and direct search filters are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_payments(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina=None, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    aggregates = compute_dashboard_aggregates(filtered, data_inicio_ccv, data_fim_ccv, data_inicio_criacao, data_fim_criacao)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = PaymentsDataPayload(
        financed_count=aggregates["payments"]["financed_count"],
        cash_count=aggregates["payments"]["cash_count"],
        financing_ratio=aggregates["payments"]["financing_ratio"],
        banks=[BankMetric(**b) for b in aggregates["payments"]["banks"]],
        methods=[PaymentMethodMetric(**m) for m in aggregates["payments"]["methods"]]
    )
    return meta

@app.get(
    "/api/dashboard/commissions",
    response_model=DashboardCommissionsResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Commission detailed metrics",
    description="Returns aggregate commission values and individual contract commission details. In live mode, period filters and direct search filters are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_commissions(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina=None, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    aggregates = compute_dashboard_aggregates(filtered, data_inicio_ccv, data_fim_ccv, data_inicio_criacao, data_fim_criacao)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = CommissionsDataPayload(
        total_commissions=aggregates["commissions"]["total_commissions"],
        avg_commission_rate=aggregates["commissions"]["avg_commission_rate"],
        commissions=[CommissionMetric(**c) for c in aggregates["commissions"]["commissions"]]
    )
    return meta

MONTHS_PT = {
    "01": "Jan", "02": "Fev", "03": "Mar", "04": "Abr", "05": "Mai", "06": "Jun",
    "07": "Jul", "08": "Ago", "09": "Set", "10": "Out", "11": "Nov", "12": "Dez"
}

@app.get(
    "/api/dashboard/timeline",
    response_model=DashboardTimelineResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Monthly Sales Timeline",
    description="Groups contract sales volume and count chronologically by month. In live mode, period filters and direct search filters are sent directly to Pipeimob CRM. Local filters (agent, category, financing, etapa_atual) are applied locally by the backend. The 'pagina' parameter is a pagination parameter and does NOT satisfy the direct filter requirement on its own. Demo mode is restricted to development and tests.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_timeline(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    pagina: Optional[int] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina=None, request_id=req_id
    )
    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    aggregates = compute_dashboard_aggregates(filtered, data_inicio_ccv, data_fim_ccv, data_inicio_criacao, data_fim_criacao)

    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = TimelineDataPayload(
        timeline=[TimelineMetric(**t) for t in aggregates["timeline"]],
        unclassified=UnclassifiedTimeline(**aggregates["unclassified"]),
        reconciliation=TimelineReconciliation(**aggregates["reconciliation"])
    )
    return meta

def warm_up_dashboard_cache():
    raw = os.getenv("DASHBOARD_WARMUP_PERIODS_JSON")
    if not raw or not raw.strip():
        return

    try:
        import json
        periods = json.loads(raw)
        if not isinstance(periods, list):
            return
    except Exception as e:
        print(f"WARMUP_ERROR: Failed to parse DASHBOARD_WARMUP_PERIODS_JSON: {e}")
        return

    import threading
    threading.Thread(target=_sequential_warmup, args=(periods,), daemon=True).start()

def _sequential_warmup(periods):
    import time
    time.sleep(2)

    data_mode, conn_status = get_current_data_mode_and_connection()
    if data_mode == "live" and conn_status == "missing_credentials":
        return

    for idx, period in enumerate(periods):
        if not isinstance(period, dict):
            continue
        start_date = period.get("start_date")
        end_date = period.get("end_date")
        if not start_date or not end_date:
            continue

        import re
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if not date_pattern.match(start_date) or not date_pattern.match(end_date):
            print(f"WARMUP_ERROR: Invalid date format: start_date={start_date}, end_date={end_date}")
            continue

        cache_key = generate_dashboard_cache_key(
            data_inicio_ccv=start_date,
            data_fim_ccv=end_date
        )
        cached_val, cache_status = dashboard_cache.get_status(cache_key)
        if cache_status in ["fresh", "stale"]:
            continue

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(load_transactions_dataset(
                data_inicio_ccv=start_date,
                data_fim_ccv=end_date,
                request_id=f"startup-warmup-{idx}"
            ))
            loop.close()
        except Exception as e:
            print(f"WARMUP_ERROR: Failed to warm up cache for {start_date} to {end_date}: {e}")

        time.sleep(1)

@app.on_event("startup")
async def startup_event():
    warm_up_dashboard_cache()

@app.get(
    "/api/dashboard/full",
    response_model=DashboardFullResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Consolidate Dashboard aggregates",
    description="Loads all transactions from Pipeimob and returns consolidated summary, origins, stages, managers, payments, commissions, and timeline aggregates in a single response.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_dashboard_full(
    response: Response,
    request: Request,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    codigo_imovel: Optional[str] = Query(None),
    codigo_contrato: Optional[str] = Query(None),
    transacao_unique_id: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None),
    etapa_atual: Optional[str] = Query(None),
    granularity: Optional[str] = Query(None),
    refresh: Optional[bool] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")

    requested_granularity = (granularity or "month").strip().lower()
    if requested_granularity not in ["auto", "day", "week", "month"]:
        raise GranularityException(
            status_code=400,
            detail=f"Invalid granularity value '{granularity}'. Supported values are: auto, day, week, month.",
            error_code="invalid_granularity"
        )

    start_str = data_inicio_ccv or data_inicio_criacao
    end_str = data_fim_ccv or data_fim_criacao

    start_dt_pre = parse_date_to_date_obj(start_str) if start_str else None
    end_dt_pre = parse_date_to_date_obj(end_str) if end_str else None

    applied_granularity_pre = None
    if start_dt_pre and end_dt_pre:
        if start_dt_pre > end_dt_pre:
            start_dt_pre, end_dt_pre = end_dt_pre, start_dt_pre
        total_days_pre = (end_dt_pre - start_dt_pre).days + 1
        if total_days_pre <= 62:
            avail_pre = ["day", "week", "month"]
        elif total_days_pre <= 366:
            avail_pre = ["week", "month"]
        else:
            avail_pre = ["month"]

        if requested_granularity == "auto":
            if total_days_pre <= 14:
                applied_granularity_pre = "day"
            elif total_days_pre <= 90:
                applied_granularity_pre = "week"
            else:
                applied_granularity_pre = "month"
        else:
            if requested_granularity not in avail_pre:
                raise GranularityException(
                    status_code=400,
                    detail=f"Granularity '{requested_granularity}' is not supported for a period of {total_days_pre} days.",
                    error_code="unsupported_granularity_for_period"
                )
            applied_granularity_pre = requested_granularity

    try:
        mode, src, dataset, pages_fetched, cache_status = await load_transactions_dataset(
            data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
            data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
            pagina=None, request_id=req_id, refresh=bool(refresh),
            requested_granularity=requested_granularity,
            applied_granularity=applied_granularity_pre
        )
    except Exception as e:
        if isinstance(e, IntegrationUnavailableError) or isinstance(e, HTTPException) or isinstance(e, GranularityException):
            raise e
        raise IntegrationUnavailableError(
            status_code=503,
            detail=f"Failed to load transactions: {e}",
            error_code="invalid_pipeimob_response",
            data_mode="live",
            pipeimob_connection="unavailable"
        )

    validate_dataset_origin(mode, src, dataset)
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )

    start_dt = start_dt_pre
    end_dt = end_dt_pre
    if not start_dt or not end_dt:
        dataset_dates = []
        for tx in filtered:
            dt_str = extract_transaction_date(tx)
            d = parse_date_to_date_obj(dt_str)
            if d:
                dataset_dates.append(d)
        if dataset_dates:
            if not start_dt:
                start_dt = min(dataset_dates)
            if not end_dt:
                end_dt = max(dataset_dates)
        else:
            today = datetime.now(ZoneInfo("America/Sao_Paulo")).date()
            if not start_dt:
                start_dt = today.replace(day=1)
            if not end_dt:
                end_dt = today

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    total_days = (end_dt - start_dt).days + 1

    if total_days <= 62:
        available_granularities = ["day", "week", "month"]
    elif total_days <= 366:
        available_granularities = ["week", "month"]
    else:
        available_granularities = ["month"]

    if requested_granularity == "auto":
        if total_days <= 14:
            applied_granularity = "day"
        elif total_days <= 90:
            applied_granularity = "week"
        else:
            applied_granularity = "month"
    else:
        if requested_granularity not in available_granularities:
            raise GranularityException(
                status_code=400,
                detail=f"Granularity '{requested_granularity}' is not supported for a period of {total_days} days.",
                error_code="unsupported_granularity_for_period"
            )
        applied_granularity = requested_granularity

    try:
        aggregates = compute_dashboard_aggregates(
            filtered,
            data_inicio_ccv, data_fim_ccv,
            data_inicio_criacao, data_fim_criacao,
            requested_granularity=requested_granularity,
            applied_granularity=applied_granularity
        )
    except Exception as e:
        if isinstance(e, GranularityException):
            raise e
        raise IntegrationUnavailableError(
            status_code=503,
            detail=f"Failed to compute dashboard aggregates: {e}",
            error_code="aggregation_failed",
            data_mode=mode,
            pipeimob_connection="internal_error"
        )

    response.headers["X-Data-Mode"] = mode
    response.headers["X-Cache"] = cache_status

    enable_debug = os.getenv("ENABLE_SAFE_DEBUG_METRICS", "false").strip().lower() == "true"
    debug_metrics = None
    if enable_debug and dataset:
        debug_metrics = {}
        debug_metrics["transaction_count"] = len(dataset)

        top_keys_counts = {}
        for tx in dataset:
            for k in tx.keys():
                top_keys_counts[k] = top_keys_counts.get(k, 0) + 1
        debug_metrics["top_level_keys_counts"] = top_keys_counts

        priority_keys = [
            "data_assinatura_ccv",
            "data_ccv",
            "data_assinatura",
            "data_contrato",
            "data_criacao",
            "created_at"
        ]

        presence_counts = {}
        type_counts = {}
        parsed_successfully = 0
        missing_count = 0
        invalid_count = 0

        nested_paths_counts = {}
        def check_nested(node, prefix_path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    current_path = f"{prefix_path}.{k}" if prefix_path else k
                    k_lower = k.lower()
                    if any(term in k_lower for term in ["data", "ccv", "date", "created", "assinatura", "criacao"]):
                        nested_paths_counts[current_path] = nested_paths_counts.get(current_path, 0) + 1
                    check_nested(v, current_path)
            elif isinstance(node, list):
                for idx, item in enumerate(node):
                    check_nested(item, f"{prefix_path}[{idx}]")

        for tx in dataset:
            check_nested(tx)

            for pk in priority_keys:
                val = tx.get(pk)
                if val is not None and val != "":
                    presence_counts[pk] = presence_counts.get(pk, 0) + 1
                    tname = type(val).__name__
                    if pk not in type_counts:
                        type_counts[pk] = {}
                    type_counts[pk][tname] = type_counts[pk].get(tname, 0) + 1

            dt_str = extract_transaction_date(tx)
            if dt_str:
                ym = parse_date_to_year_month(dt_str)
                if ym:
                    parsed_successfully += 1
                else:
                    invalid_count += 1
            else:
                missing_count += 1

        debug_metrics["priority_keys_presence"] = presence_counts
        debug_metrics["priority_keys_types"] = type_counts
        debug_metrics["nested_paths_counts"] = nested_paths_counts
        debug_metrics["parsed_successfully"] = parsed_successfully
        debug_metrics["missing_count"] = missing_count
        debug_metrics["invalid_count"] = invalid_count

        debug_metrics["stages_validation"] = {
            "raw_count": len(dataset),
            "normalized_count": len(dataset),
            "sanitized_count": len([sanitize_transaction(tx) for tx in dataset]),
            "aggregator_count": len(filtered)
        }

    generated_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    filters_map = {
        "data_inicio_ccv": data_inicio_ccv,
        "data_fim_ccv": data_fim_ccv,
        "data_inicio_criacao": data_inicio_criacao,
        "data_fim_criacao": data_fim_criacao,
        "codigo_imovel": codigo_imovel,
        "codigo_contrato": codigo_contrato,
        "transacao_unique_id": transacao_unique_id,
        "agent": agent,
        "category": category,
        "financing": financing,
        "etapa_atual": etapa_atual,
        "granularity": granularity
    }
    filters_applied = {k: v for k, v in filters_map.items() if v is not None}

    return DashboardFullResponse(
        data_mode=mode,
        source=src,
        period=DashboardPeriod(start=data_inicio_ccv, end=data_fim_ccv),
        requested_granularity=requested_granularity,
        applied_granularity=applied_granularity,
        available_granularities=available_granularities,
        pages_fetched=pages_fetched,
        transaction_count=len(filtered),
        summary=SummaryDataPayload(**aggregates["summary"]),
        origins=[OriginMetric(**o) for o in aggregates["origins"]],
        stages=[StageMetric(**s) for s in aggregates["stages"]],
        managers=[ManagerMetric(**m) for m in aggregates["managers"]],
        payments=PaymentsDataPayload(**aggregates["payments"]),
        commissions=CommissionsDataPayload(**aggregates["commissions"]),
        timeline=[DashboardTimelineMetric(**t) for t in aggregates["timeline"]],
        unclassified=UnclassifiedTimeline(**aggregates["unclassified"]),
        reconciliation=TimelineReconciliation(**aggregates["reconciliation"]),
        sales_cycle=SalesCyclePayload(**aggregates["sales_cycle"]) if aggregates.get("sales_cycle") is not None else None,
        schema_version="1.0",
        generated_at=generated_at_utc,
        filters_applied=filters_applied,
        commission_financials=CommissionFinancials(**aggregates["commission_financials"]),
        debug_metrics=debug_metrics,
        data_quality=DataQualityPayload(**aggregates["data_quality"]) if aggregates.get("data_quality") is not None else None
    )


@app.get(
    "/api/reconciliation/sales",
    dependencies=[Depends(verify_backend_api_key)],
    summary="Reconcile Pipeimob sales with Vista gains",
    description=(
        "Uses Pipeimob as the official source for sale count, CCV date and VGV, "
        "then enriches matched contracts with the commercial ownership available "
        "in Vista. Client personal data is neither requested nor returned."
    ),
)
async def get_sales_reconciliation(
    request: Request,
    response: Response,
    data_inicio_ccv: str = Query(..., description="Official CCV start date (YYYY-MM-DD)"),
    data_fim_ccv: str = Query(..., description="Official CCV end date (YYYY-MM-DD)"),
    date_tolerance_days: int = Query(7, ge=0, le=31),
    refresh: bool = Query(False),
):
    try:
        start_date = date.fromisoformat(data_inicio_ccv)
        end_date = date.fromisoformat(data_fim_ccv)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="CCV dates must use YYYY-MM-DD"
        ) from exc
    if start_date > end_date:
        raise HTTPException(
            status_code=400, detail="data_inicio_ccv cannot be after data_fim_ccv"
        )

    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    mode, source, dataset, pages_fetched, cache_status = await load_transactions_dataset(
        data_inicio_ccv=data_inicio_ccv,
        data_fim_ccv=data_fim_ccv,
        request_id=request_id,
        refresh=bool(refresh),
    )
    validate_dataset_origin(mode, source, dataset)
    if mode != "live" or source != "pipeimob_api_v2":
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Official sales reconciliation requires live Pipeimob data.",
            error_code="reconciliation_requires_live_pipeimob",
            data_mode=mode,
            pipeimob_connection="unavailable",
        )

    official_transactions = []
    for transaction in dataset:
        signed_date = parse_date_to_date_obj(
            pipeimob_official_sale_date(transaction)
        )
        if signed_date and start_date <= signed_date <= end_date:
            official_transactions.append(transaction)

    try:
        vista_client = VistaSalesClient.from_env()
        vista_start = start_date - timedelta(days=date_tolerance_days)
        vista_end = end_date + timedelta(days=date_tolerance_days)
        vista_gains = await asyncio.to_thread(
            vista_client.fetch_gains, vista_start, vista_end
        )
    except VistaSalesConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail="Vista sales reconciliation is not configured.",
            headers={"X-Reconciliation-Error": "vista_not_configured"},
        ) from exc
    except VistaSalesAPIError as exc:
        raise HTTPException(
            status_code=503,
            detail="Vista sales data is temporarily unavailable.",
            headers={"X-Reconciliation-Error": "vista_unavailable"},
        ) from exc

    official_property_codes = {
        str(transaction.get("codigo_imovel") or "").strip().upper()
        for transaction in official_transactions
        if transaction.get("codigo_imovel") not in (None, "")
    }
    relevant_vista_gains = []
    for gain in vista_gains:
        gain_date = parse_date_to_date_obj(gain.get("gain_date"))
        property_code = str(gain.get("property_code") or "").strip().upper()
        if property_code in official_property_codes or (
            gain_date and start_date <= gain_date <= end_date
        ):
            relevant_vista_gains.append(gain)

    result = reconcile_sales(
        official_transactions,
        relevant_vista_gains,
        date_tolerance_days=date_tolerance_days,
    )
    result.update(
        {
            "period": {"start": data_inicio_ccv, "end": data_fim_ccv},
            "generated_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "pipeimob_pages_fetched": pages_fetched,
            "pipeimob_cache_status": cache_status,
            "vista_pipe_id_configured": True,
            "commercial_broker_limitation": (
                "The documented Vista negocios/listar fields do not guarantee a "
                "commercial broker field. Missing ownership remains pending and is "
                "never replaced by Pipeimob's manager."
            ),
        }
    )
    response.headers["X-Data-Mode"] = "live"
    response.headers["X-Reconciliation-Contract"] = result["contract_version"]
    return result


# ======================================================================
# CONTRACTS CONTROL (SECRETARIA DE VENDAS) BI MODULE
# ======================================================================

from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import date, datetime
import math

class ContractsControlPeriod(BaseModel):
    start: Optional[str] = None
    end: Optional[str] = None
    basis: str = "data_inicio_venda"
    as_of_date: str

class ContractsControlExtraction(BaseModel):
    upstream_endpoint: str
    upstream_filter_field: str
    coverage_start: str
    coverage_end: str
    pages_fetched: int
    raw_records_fetched: int
    coverage_status: str

class ContractsControlCohortSummary(BaseModel):
    records_count: int
    completed_count: int
    in_progress_count: int
    data_issue_count: int
    cancelled_count: int
    average_duration_days: float
    median_duration_days: float
    p75_duration_days: float
    p90_duration_days: float
    average_open_aging_days: float
    median_open_aging_days: float
    without_manager_count: int
    unknown_modality_count: int

class ContractsControlOperationsSummary(BaseModel):
    scope: str = "operations"
    opening_backlog_count: int
    period_started_count: int
    period_completed_count: int
    ending_backlog_count: int
    excluded_data_issue_count: int
    provisional: bool = True

class ContractsControlBucket(BaseModel):
    scope: str = "cohort"
    key: str
    label: str
    count: int
    ratio: float

class ContractsControlResponsibleMetric(BaseModel):
    scope: str = "cohort"
    rank: Optional[int] = None
    responsible_id: Optional[str] = None
    responsible: Optional[str] = None
    ranking_eligible: bool = False
    ranking_basis: str = "average_duration_days_completed"
    records_count: int
    completed_count: int
    in_progress_count: int
    current_in_progress_count: int
    data_issue_count: int
    average_duration_days: float
    median_duration_days: float
    p75_duration_days: float
    average_open_aging_days: float
    median_open_aging_days: float
    completion_ratio: float
    unknown_modality_count: int

class ContractsControlManagerMetric(BaseModel):
    scope: str = "cohort"
    manager: Optional[str] = None
    records_count: int
    completed_count: int
    in_progress_count: int
    data_issue_count: int
    average_duration_days: float
    median_duration_days: float
    p75_duration_days: float
    average_open_aging_days: float
    median_open_aging_days: float
    completion_ratio: float
    unknown_modality_count: int

class ContractsControlModalitySummary(BaseModel):
    scope: str = "cohort"
    financing_count: int
    deed_count: int
    developer_payment_count: int
    unknown_modality_count: int
    conflict_count: int
    financing_amount_known_count: int
    financing_amount_unknown_count: int
    financing_ratio_known_count: int
    financing_total_amount: Optional[float] = None
    average_financing_ratio: Optional[float] = None

class ContractsControlSourceTypeMetric(BaseModel):
    scope: str = "cohort"
    source_type: Optional[str] = None
    label: str
    count: int
    ratio: float

class ContractsControlTimelineMetric(BaseModel):
    scope: str = "operations"
    month: str
    label: str
    opening_backlog: int
    started_count: int
    completed_count: int
    net_flow: int
    ending_backlog: int
    excluded_data_issue_count: int
    average_duration_days: float
    median_duration_days: float
    provisional: bool = True

class ContractsControlWeeklyResponsibleMetric(BaseModel):
    responsible: Optional[str] = None
    started_count: int

class ContractsControlWeeklyTimelineMetric(BaseModel):
    scope: str = "operations"
    week_start: str
    week_end: str
    label: str
    opening_backlog: int
    started_count: int
    completed_count: int
    net_flow: int
    ending_backlog: int
    assigned_started_count: int
    unassigned_started_count: int
    assignment_completion_ratio: float
    by_responsible: List[ContractsControlWeeklyResponsibleMetric]
    excluded_data_issue_count: int
    average_duration_days: float
    median_duration_days: float
    provisional: bool = True

class ContractsControlDataQuality(BaseModel):
    scope: str = "cohort"
    records_count: int
    valid_records_count: int
    missing_start_date_count: int
    invalid_start_date_count: int
    open_without_contract_date_count: int
    invalid_contract_date_count: int
    negative_duration_count: int
    future_start_date_count: int
    missing_manager_count: int
    missing_financing_field_count: int
    mapping_status: Dict[str, str]

class ContractsControlExtractionQuality(BaseModel):
    raw_records_count: int
    unique_records_count: int
    duplicate_transaction_count: int
    duplicate_conflict_count: int
    duplicate_resolution_policy: str = "first_api_occurrence"

class ContractsControlManualEnrichment(BaseModel):
    status: str
    scope: str = "operations"
    eligible_records_count: Optional[int] = None
    responsible_filled_count: Optional[int] = None
    responsible_pending_count: Optional[int] = None
    responsible_completion_ratio: Optional[float] = None
    last_manual_update_at: Optional[datetime] = None

class ContractsControlSummaryResponse(BaseModel):
    period: ContractsControlPeriod
    extraction: ContractsControlExtraction
    extraction_quality: ContractsControlExtractionQuality
    cohort_summary: ContractsControlCohortSummary
    operations_summary: ContractsControlOperationsSummary
    aging_buckets: List[ContractsControlBucket]
    open_sla_buckets: List[ContractsControlBucket]
    duration_buckets: List[ContractsControlBucket]
    by_responsible: List[ContractsControlResponsibleMetric]
    by_manager: List[ContractsControlManagerMetric]
    by_modality: ContractsControlModalitySummary
    by_source_type: List[ContractsControlSourceTypeMetric]
    timeline: List[ContractsControlTimelineMetric]
    weekly_timeline: List[ContractsControlWeeklyTimelineMetric]
    data_quality: ContractsControlDataQuality
    manual_enrichment: ContractsControlManualEnrichment

class ContractsControlResponsibleReference(BaseModel):
    id: str
    name: str
    active: bool

class ContractsControlDeal(BaseModel):
    transaction_id: str
    property_code: str
    property_title: Optional[str] = None
    property_title_source: str = "unavailable"
    start_date: Optional[str] = None
    contract_date: Optional[str] = None
    duration_days: Optional[int] = None
    current_aging_days: Optional[int] = None
    aging_days_at_period_end: Optional[int] = None
    elapsed_days: Optional[int] = None
    sla_bucket: str
    sla_label: str
    sla_action: str
    manager: Optional[str] = None
    responsible: Optional[ContractsControlResponsibleReference] = None
    modality: str
    modality_label: str
    modality_source: str
    modality_confidence: str
    financing_bank: Optional[str] = None
    financing_amount: Optional[float] = None
    financing_ratio: Optional[float] = None
    modality_flags: List[str]
    source_type: str
    source_type_label: str
    current_status: str
    status_at_period_end: str
    data_quality_flags: List[str]
    period_roles: Optional[List[str]] = None
    manual_data_version: int = 0

class ContractsControlDealsResponse(BaseModel):
    page: int
    page_size: int
    total_records: int
    total_pages: int
    deals: List[ContractsControlDeal]

class ContractsControlResponsiblesResponse(BaseModel):
    responsibles: List[ContractsControlResponsibleReference]

class CreateResponsibleRequest(BaseModel):
    name: str

class UpdateResponsibleRequest(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None

class UpdateIndividualAttributionRequest(BaseModel):
    responsible_id: Optional[str] = None
    version: int

class IndividualAttributionResponse(BaseModel):
    transaction_id: str
    responsible: Optional[ContractsControlResponsibleReference] = None
    version: int
    updated_at: str
    changed: bool

class BulkAttributionItem(BaseModel):
    transaction_id: str
    version: int

class BulkAttributionRequest(BaseModel):
    items: List[BulkAttributionItem]
    responsible_id: Optional[str] = None

class BulkAttributionItemResponse(BaseModel):
    transaction_id: str
    version: int
    changed: bool

class BulkAttributionResponse(BaseModel):
    requested_count: int
    updated_count: int
    unchanged_count: int
    items: List[BulkAttributionItemResponse]

class HistoryResponsibleReference(BaseModel):
    id: str
    current_name: str = Field(description="The current name of the responsible in the database register.")
    active: bool

class HistoryRecordItem(BaseModel):
    field_name: str
    previous_responsible: Optional[HistoryResponsibleReference] = None
    new_responsible: Optional[HistoryResponsibleReference] = None
    previous_version: Optional[int] = None
    new_version: int
    changed_at: str
    changed_by_sub: str

# Cache and settings
CONTRACTS_CONTROL_CACHE_VERSION = "contracts-control-v2-weekly-operations"

class ContractsControlCache:
    def __init__(self):
        from threading import Lock
        self.cache = {}
        self.lock = Lock()

    def get_status(self, key):
        with self.lock:
            if key in self.cache:
                entry = self.cache[key]
                now = time.time()
                if len(entry) == 4:
                    val, fresh_until, stale_until, max_stale_until = entry
                else:
                    val, fresh_until, stale_until = entry
                    max_stale_until = stale_until

                if now <= fresh_until:
                    return val, "fresh"
                elif now <= stale_until:
                    return val, "stale"
                elif now <= max_stale_until:
                    return val, "expired"
                else:
                    del self.cache[key]
            return None, "miss"

    def get(self, key):
        val, status = self.get_status(key)
        return val

    def set(self, key, val, ttl=300):
        with self.lock:
            now = time.time()
            max_stale_env = os.getenv("CONTRACTS_CONTROL_MAX_STALE_SECONDS")
            try:
                max_stale_seconds = int(max_stale_env) if max_stale_env else 86400
            except ValueError:
                max_stale_seconds = 86400
            if max_stale_seconds <= 0:
                max_stale_seconds = 86400

            self.cache[key] = (
                val,
                now + ttl,
                now + ttl + DASHBOARD_STALE_TTL_SECONDS,
                now + ttl + max_stale_seconds
            )

    def clear(self):
        with self.lock:
            self.cache.clear()

    def clear_endpoint_caches(self):
        with self.lock:
            keys_to_remove = [
                k for k in self.cache.keys()
                if isinstance(k, tuple) and len(k) > 0 and k[0] == "contracts-control"
            ]
            for k in keys_to_remove:
                self.cache.pop(k, None)

contracts_control_cache = ContractsControlCache()

def generate_contracts_control_cache_key(coverage_start: str) -> tuple:
    return ("pipeimob:raw", CONTRACTS_CONTROL_CACHE_VERSION, coverage_start)

# Strict date parsing and stats helpers - parse_date_to_date_obj defined earlier in module

def contracts_control_calculate_percentile(values: List[float], percentile: float) -> float:
    if not values:
        return 0.0
    return calculate_percentile(sorted(values), percentile / 100.0)

def contracts_control_calculate_median(values: List[float]) -> float:
    return contracts_control_calculate_percentile(values, 50.0)

def calculate_average(values: List[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))

def deduplicate_contracts_control_dataset(dataset: list) -> tuple[list, int, int]:
    seen = {}
    unique_list = []
    duplicate_count = 0
    conflict_count = 0

    for tx in dataset:
        tx_id = tx.get("transacao_unique_id_pipeimob")
        if not tx_id:
            unique_list.append(tx)
            continue

        if tx_id not in seen:
            seen[tx_id] = tx
            unique_list.append(tx)
        else:
            duplicate_count += 1
            first_tx = seen[tx_id]
            diverged = (
                first_tx.get("codigo_imovel") != tx.get("codigo_imovel") or
                first_tx.get("data_inicio_venda") != tx.get("data_inicio_venda") or
                first_tx.get("data_contrato") != tx.get("data_contrato") or
                first_tx.get("agente_gestor") != tx.get("agente_gestor") or
                first_tx.get("financiamento") != tx.get("financiamento")
            )
            if diverged:
                conflict_count += 1

    return unique_list, duplicate_count, conflict_count

def normalize_text(text: Any) -> str:
    import unicodedata
    if text is None:
        return ""
    s = str(text).strip().lower()
    s = "".join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    return s

def build_contracts_control_property_title(tx: dict) -> tuple[Optional[str], str]:
    """Build a safe API-derived property label when Pipeimob has no title field."""
    category = str(tx.get("categoria_crm") or tx.get("categoria") or "").strip()
    street = str(tx.get("endereco_logradouro") or "").strip()
    number = str(tx.get("endereco_numero") or "").strip()
    complement = str(tx.get("endereco_complemento") or "").strip()

    address_parts = []
    if street:
        address_parts.append(street)
    if number:
        address_parts.append(number)
    if complement:
        address_parts.append(complement)

    address = ", ".join(address_parts)
    if category and address:
        return f"{category} · {address}", "derived_api"
    if address:
        return address, "derived_api"
    if category:
        return category, "derived_api"
    return None, "unavailable"

def classify_signature_sla(elapsed_days: Optional[int], current_status: str) -> dict:
    """Classify the 30/60/90-day signature SLA and return an operational action."""
    if elapsed_days is None or elapsed_days < 0 or current_status == "data_issue":
        return {
            "sla_bucket": "data_issue",
            "sla_label": "Revisar dados",
            "sla_action": "Corrigir as datas de cadastro e assinatura antes de acompanhar o prazo."
        }

    if elapsed_days < 30:
        bucket = "under_30_days"
        label = "Até 29 dias"
        open_action = "Acompanhar no fluxo normal."
    elif elapsed_days < 60:
        bucket = "30_59_days"
        label = "30 a 59 dias"
        open_action = "Revisar pendências com o responsável."
    elif elapsed_days < 90:
        bucket = "60_89_days"
        label = "60 a 89 dias"
        open_action = "Definir plano de ação com o responsável e o gerente."
    else:
        bucket = "90_plus_days"
        label = "90 dias ou mais"
        open_action = "Escalonar para a gestão e priorizar a assinatura."

    return {
        "sla_bucket": bucket,
        "sla_label": label,
        "sla_action": (
            open_action
            if current_status == "in_progress"
            else "Processo concluído; considerar no histórico de desempenho."
        )
    }

def classify_contract_modality(tx: dict) -> dict:
    midia_raw = tx.get("midia_origem_vendedores")
    midia_norm = normalize_text(midia_raw)

    if midia_norm in ("terceiros prontos", "terceiros obras"):
        source_type = "third_party"
        source_type_label = "Terceiros"
    elif midia_norm == "construtora obra":
        source_type = "launch"
        source_type_label = "Lançamento"
    else:
        source_type = "unknown"
        source_type_label = "Não classificado"

    fin_field = tx.get("financiamento")
    if isinstance(fin_field, str):
        if fin_field.lower() == "true":
            is_fin_true = True
            is_fin_false = False
            is_fin_none = False
        elif fin_field.lower() == "false":
            is_fin_true = False
            is_fin_false = True
            is_fin_none = False
        else:
            is_fin_true = False
            is_fin_false = False
            is_fin_none = True
    else:
        is_fin_true = fin_field is True
        is_fin_false = fin_field is False
        is_fin_none = fin_field is None

    bank_raw = tx.get("financiamento_banco")
    has_bank = False
    financing_bank = None
    if bank_raw is not None:
        bank_str = str(bank_raw).strip()
        if bank_str != "" and bank_str.lower() not in ("false", "none", "null"):
            has_bank = True
            financing_bank = bank_str

    has_financing_payment = False
    financing_payment_amount = 0.0
    has_deed_payment = False

    forma_pagto = tx.get("forma_pagamento") or []
    if isinstance(forma_pagto, list):
        for fp in forma_pagto:
            if not isinstance(fp, dict):
                continue
            fp_nome = normalize_text(fp.get("forma_pagamento_nome"))
            try:
                val_raw = fp.get("forma_pagamento_valor")
                fp_valor = float(val_raw) if val_raw is not None else 0.0
            except (ValueError, TypeError):
                fp_valor = 0.0

            if fp_valor > 0:
                if ("financiamento" in fp_nome or
                    "alienacao fiduciaria" in fp_nome or
                    "credito do financiamento" in fp_nome or
                    "contrato bancario" in fp_nome):
                    has_financing_payment = True
                    financing_payment_amount += fp_valor
                if "escritura" in fp_nome:
                    has_deed_payment = True

    contract_val_raw = tx.get("valor_contrato")
    try:
        valor_contrato = float(contract_val_raw) if contract_val_raw is not None else 0.0
    except (ValueError, TypeError):
        valor_contrato = 0.0

    financing_amount = None
    financing_ratio = None
    if has_financing_payment:
        financing_amount = financing_payment_amount
        if valor_contrato > 0:
            financing_ratio = float(financing_amount / valor_contrato)

    modality_flags = []
    is_conflict = False

    if is_fin_false and has_bank:
        is_conflict = True
        modality_flags.append("conflict_financing_false_with_bank")
    if is_fin_false and has_financing_payment:
        is_conflict = True
        modality_flags.append("conflict_financing_false_with_payment")

    if is_fin_true:
        modality = "financing"
        modality_label = "Financiamento"
        modality_confidence = "confirmed"
        modality_source = "api_field"
        if not has_bank and not has_financing_payment:
            modality_flags.append("financing_details_incomplete")

    elif is_conflict:
        modality = "financing"
        modality_label = "Financiamento"
        modality_confidence = "conflict"
        modality_source = "conflict"

    elif is_fin_none and (has_bank or has_financing_payment):
        modality = "financing"
        modality_label = "Financiamento"
        modality_confidence = "inferred"
        modality_source = "api_field" if has_bank else "payment_terms"

    elif source_type == "launch":
        modality = "developer_payment"
        modality_label = "Pagamento Direto Construtora"
        modality_confidence = "inferred"
        modality_source = "source_type_launch"
        if is_fin_false:
            modality_flags.append("launch_without_bank_financing")

    elif is_fin_false or has_deed_payment:
        modality = "deed"
        modality_label = "Escritura"
        if has_deed_payment:
            modality_confidence = "confirmed"
            modality_source = "payment_terms"
        else:
            modality_confidence = "inferred"
            modality_source = "inferred"

    else:
        modality = "unknown"
        modality_label = "Não classificado"
        modality_confidence = "inferred"
        modality_source = "none"

    return {
        "modality": modality,
        "modality_label": modality_label,
        "modality_source": modality_source,
        "modality_confidence": modality_confidence,
        "financing_bank": financing_bank,
        "financing_amount": financing_amount,
        "financing_ratio": financing_ratio,
        "modality_flags": modality_flags,
        "source_type": source_type,
        "source_type_label": source_type_label
    }

def classify_contracts_control_process(tx: dict, as_of_date_obj: date, end_date_obj: date) -> dict:
    start_str = tx.get("data_inicio_venda")
    contract_str = tx.get("data_contrato")

    start_date_obj = parse_date_to_date_obj(start_str)
    start_has_raw = start_str is not None and str(start_str).strip() != ""
    start_is_invalid = start_has_raw and start_date_obj is None

    contract_date_obj = parse_date_to_date_obj(contract_str)
    contract_has_raw = contract_str is not None and str(contract_str).strip() != ""
    contract_is_invalid = contract_has_raw and contract_date_obj is None

    def get_status_at(ref_date: date):
        if not start_has_raw:
            return "data_issue"
        if start_is_invalid:
            return "data_issue"
        if contract_is_invalid:
            return "data_issue"
        if start_date_obj and start_date_obj > ref_date:
            if start_date_obj > as_of_date_obj:
                return "data_issue"
            return "future"
        if contract_date_obj and contract_date_obj < start_date_obj:
            return "data_issue"

        if contract_date_obj is None or contract_date_obj > ref_date:
            return "in_progress"
        else:
            return "completed"

    status_at_period_end = get_status_at(end_date_obj)
    current_status = get_status_at(as_of_date_obj)

    data_quality_flags = set()
    if not start_has_raw:
        data_quality_flags.add("missing_start_date")
    if start_is_invalid:
        data_quality_flags.add("invalid_start_date")
    if contract_is_invalid:
        data_quality_flags.add("invalid_contract_date")
    if start_date_obj and start_date_obj > as_of_date_obj:
        data_quality_flags.add("future_start_date")
    if start_date_obj and contract_date_obj and contract_date_obj < start_date_obj:
        data_quality_flags.add("negative_duration")

    financiamento = tx.get("financiamento")
    if financiamento is None:
        data_quality_flags.add("missing_financing_field")

    manager = tx.get("agente_gestor")
    if manager is None or str(manager).strip() == "":
        data_quality_flags.add("missing_manager")

    duration_days = None
    if start_date_obj and contract_date_obj and contract_date_obj >= start_date_obj and not contract_is_invalid and not start_is_invalid:
        duration_days = (contract_date_obj - start_date_obj).days

    current_aging_days = None
    if current_status == "in_progress" and start_date_obj:
        current_aging_days = (as_of_date_obj - start_date_obj).days

    aging_days_at_period_end = None
    if status_at_period_end == "in_progress" and start_date_obj:
        aging_days_at_period_end = (end_date_obj - start_date_obj).days

    elapsed_days = None
    if current_status == "completed":
        elapsed_days = duration_days
    elif current_status == "in_progress":
        elapsed_days = current_aging_days

    sla_info = classify_signature_sla(elapsed_days, current_status)

    modality_info = classify_contract_modality(tx)

    res_dict = {
        "start_date_obj": start_date_obj,
        "contract_date_obj": contract_date_obj,
        "current_status": current_status,
        "status_at_period_end": status_at_period_end,
        "duration_days": duration_days,
        "current_aging_days": current_aging_days,
        "aging_days_at_period_end": aging_days_at_period_end,
        "elapsed_days": elapsed_days,
        "data_quality_flags": sorted(list(data_quality_flags))
    }
    res_dict.update(sla_info)
    res_dict.update(modality_info)
    return res_dict

def _refresh_contracts_control_dataset(request_id=None, caller_endpoint=None):
    import time
    import json
    import logging
    import os
    logger = logging.getLogger(__name__)
    coverage_start = "2020-01-01"
    cache_key = generate_contracts_control_cache_key(coverage_start)

    api_key = os.getenv("PIPEIMOB_API_KEY").strip()
    api_secret = os.getenv("PIPEIMOB_SECRET_KEY").strip()

    logger.info(json.dumps({
        "event": "external_call_start",
        "request_id": request_id,
        "caller_endpoint": caller_endpoint,
        "timeout_configured": PIPEIMOB_HTTP_TIMEOUT_SECONDS
    }))

    ext_start = time.perf_counter()
    try:
        txs, pages = fetch_all_pipeimob_transactions(
            api_key=api_key,
            api_secret=api_secret,
            data_inicio_criacao=coverage_start
        )
        ext_duration = (time.perf_counter() - ext_start) * 1000

        logger.info(json.dumps({
            "event": "external_call_end",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "external_duration_ms": ext_duration,
            "external_status": "success",
            "records_count": len(txs)
        }))
    except Exception as err:
        ext_duration = (time.perf_counter() - ext_start) * 1000
        logger.info(json.dumps({
            "event": "external_call_end",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "external_duration_ms": ext_duration,
            "external_status": "error",
            "exception_class": type(err).__name__,
            "sanitized_message": str(err)
        }))
        raise err

    if not txs:
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Pipeimob CRM API returned empty transactions dataset for global contracts control.",
            error_code="invalid_pipeimob_response",
            data_mode="live",
            pipeimob_connection="unavailable"
        )

    logger.info(json.dumps({
        "event": "transformation_start",
        "request_id": request_id,
        "caller_endpoint": caller_endpoint
    }))

    trans_start = time.perf_counter()
    trans_duration = (time.perf_counter() - trans_start) * 1000
    logger.info(json.dumps({
        "event": "transformation_end",
        "request_id": request_id,
        "caller_endpoint": caller_endpoint,
        "records_count": len(txs),
        "transformation_duration_ms": trans_duration
    }))

    contracts_control_cache.set(cache_key, (txs, pages))
    return txs, pages

async def load_contracts_control_dataset(
    request_id: Optional[str] = None,
    refresh: bool = False,
    caller_endpoint: Optional[str] = None
) -> tuple:
    import time
    import json
    import logging
    logger = logging.getLogger(__name__)

    start_time = time.perf_counter()

    logger.info(json.dumps({
        "event": "load_start",
        "request_id": request_id,
        "caller_endpoint": caller_endpoint,
        "refresh": refresh
    }))

    data_mode, conn_status = get_current_data_mode_and_connection()

    if data_mode == "unconfigured":
        logger.info(json.dumps({
            "event": "error",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "exception_class": "IntegrationUnavailableError",
            "sanitized_message": "Configuration pending."
        }))
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Configuration pending. Please set PIPEIMOB_DATA_MODE environment variable.",
            error_code="integration_unconfigured",
            data_mode="unconfigured",
            pipeimob_connection="pending_configuration"
        )

    if data_mode == "demo":
        from mock_data import MOCK_TRANSACTIONS
        dataset = MOCK_TRANSACTIONS
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(json.dumps({
            "event": "load_end",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "cache_status": "miss",
            "records_count": len(dataset),
            "total_duration_ms": duration_ms
        }))
        return "demo", "synthetic_mock", dataset, 1, "miss"

    if conn_status == "missing_credentials":
        logger.info(json.dumps({
            "event": "error",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "exception_class": "IntegrationUnavailableError",
            "sanitized_message": "Pipeimob credentials are not configured on the server."
        }))
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Pipeimob credentials are not configured on the server.",
            error_code="missing_credentials",
            data_mode="live",
            pipeimob_connection="missing_credentials"
        )

    coverage_start = "2020-01-01"
    cache_key = generate_contracts_control_cache_key(coverage_start)

    # Timeouts configuration
    warmup_wait_env = os.getenv("CONTRACTS_CONTROL_WARMUP_WAIT_SECONDS")
    try:
        warmup_wait_seconds = float(warmup_wait_env) if warmup_wait_env else 5.0
    except ValueError:
        warmup_wait_seconds = 5.0
    if warmup_wait_seconds <= 0:
        warmup_wait_seconds = 5.0

    retry_after_env = os.getenv("CONTRACTS_CONTROL_RETRY_AFTER_SECONDS")
    try:
        retry_after_seconds = int(retry_after_env) if retry_after_env else 30
    except ValueError:
        retry_after_seconds = 30
    if retry_after_seconds <= 0:
        retry_after_seconds = 30

    if refresh:
        logger.info(json.dumps({
            "event": "cache_check",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "cache_status": "miss"
        }))
        coro = lambda: asyncio.get_event_loop().run_in_executor(
            None, _refresh_contracts_control_dataset, request_id, caller_endpoint
        )
        try:
            live_txs, pages_fetched = await single_flight_registry.execute(
                cache_key, coro, request_id, caller_endpoint, timeout=warmup_wait_seconds
            )
            cache_status = "miss"
        except asyncio.TimeoutError:
            raise DatasetWarmingError(retry_after=retry_after_seconds)
    else:
        cached_val, status = contracts_control_cache.get_status(cache_key)
        logger.info(json.dumps({
            "event": "cache_check",
            "request_id": request_id,
            "caller_endpoint": caller_endpoint,
            "cache_status": status
        }))
        if status == "fresh":
            live_txs, pages_fetched = cached_val
            cache_status = "fresh"
        elif status in ("stale", "expired"):
            live_txs, pages_fetched = cached_val
            cache_status = "stale"
            coro = lambda: asyncio.get_event_loop().run_in_executor(
                None, _refresh_contracts_control_dataset, "bg-warmup", "background_refresh"
            )
            single_flight_registry.start_background(cache_key, coro)
        else:
            coro = lambda: asyncio.get_event_loop().run_in_executor(
                None, _refresh_contracts_control_dataset, request_id, caller_endpoint
            )
            try:
                live_txs, pages_fetched = await single_flight_registry.execute(
                    cache_key, coro, request_id, caller_endpoint, timeout=warmup_wait_seconds
                )
                cache_status = "miss"
            except asyncio.TimeoutError:
                raise DatasetWarmingError(retry_after=retry_after_seconds)

    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.info(json.dumps({
        "event": "load_end",
        "request_id": request_id,
        "caller_endpoint": caller_endpoint,
        "cache_status": cache_status,
        "records_count": len(live_txs),
        "total_duration_ms": duration_ms
    }))

    return "live", "pipeimob_api_v2", live_txs, pages_fetched, cache_status

async def get_contracts_control_dataset_for_write() -> list:
    import sys
    if "pytest" in sys.modules:
        mode, src, dataset, pages, cache = await load_contracts_control_dataset(refresh=False)
        return dataset

    data_mode, conn_status = get_current_data_mode_and_connection()
    if data_mode == "demo":
        from mock_data import MOCK_TRANSACTIONS
        return MOCK_TRANSACTIONS

    if data_mode == "live":
        cache_key = generate_contracts_control_cache_key("2020-01-01")
        cached_val, status = contracts_control_cache.get_status(cache_key)
        if status in ("fresh", "stale"):
            txs, pages = cached_val
            return txs

        # Cache is empty (miss) -> fail fast
        raise HTTPException(
            status_code=503,
            detail="Pipeimob dataset cache is empty. Please warm up the cache by calling read endpoints first."
        )

    raise HTTPException(
        status_code=503,
        detail="Pipeimob integration is unconfigured or unavailable."
    )

def generate_months_between(start_date_obj: date, end_date_obj: date) -> list[str]:
    months = []
    curr = start_date_obj.replace(day=1)
    end_limit = end_date_obj.replace(day=1)
    while curr <= end_limit:
        months.append(curr.strftime("%Y-%m"))
        if curr.month == 12:
            curr = curr.replace(year=curr.year + 1, month=1)
        else:
            curr = curr.replace(month=curr.month + 1)
    return months

def generate_contracts_control_weeks(start_date_obj: date, end_date_obj: date) -> list[tuple[date, date]]:
    """Return Monday-to-Sunday operational weeks clipped to the selected period."""
    weeks = []
    curr_start = start_date_obj
    while curr_start <= end_date_obj:
        curr_end = min(curr_start + timedelta(days=6 - curr_start.weekday()), end_date_obj)
        weeks.append((curr_start, curr_end))
        curr_start = curr_end + timedelta(days=1)
    return weeks

def format_contracts_control_week_label(week_start: date, week_end: date) -> str:
    months_pt = {
        1: "jan", 2: "fev", 3: "mar", 4: "abr", 5: "mai", 6: "jun",
        7: "jul", 8: "ago", 9: "set", 10: "out", 11: "nov", 12: "dez"
    }
    if week_start.year != week_end.year:
        return (
            f"{week_start.day:02d} {months_pt[week_start.month]} {week_start.year}–"
            f"{week_end.day:02d} {months_pt[week_end.month]} {week_end.year}"
        )
    if week_start.month != week_end.month:
        return (
            f"{week_start.day:02d} {months_pt[week_start.month]}–"
            f"{week_end.day:02d} {months_pt[week_end.month]}"
        )
    return f"{week_start.day:02d}–{week_end.day:02d} {months_pt[week_start.month]}"

def get_duration_bucket(days: int) -> str:
    if days <= 3: return "0_3_days"
    if days <= 7: return "4_7_days"
    if days <= 15: return "8_15_days"
    if days <= 30: return "16_30_days"
    if days <= 60: return "31_60_days"
    return "over_60_days"

def get_aging_bucket(days: int) -> str:
    if days <= 3: return "0_3_days"
    if days <= 7: return "4_7_days"
    if days <= 15: return "8_15_days"
    if days <= 30: return "16_30_days"
    return "over_30_days"

def matches_contracts_control_aging_filter(item: dict, requested_bucket: str) -> bool:
    sla_buckets = {"under_30_days", "30_59_days", "60_89_days", "90_plus_days"}
    if requested_bucket in sla_buckets:
        return (
            item.get("current_status") == "in_progress"
            and item.get("sla_bucket") == requested_bucket
        )

    current_aging_days = item.get("current_aging_days")
    if current_aging_days is None:
        return False
    return get_aging_bucket(current_aging_days) == requested_bucket

# Main Core Aggregator
def compute_contracts_control_data(
    dataset: list,
    start_date_str: str,
    end_date_str: str,
    as_of_date_str: str,
    pre_classified_txs: Optional[list] = None,
    duplicate_count: int = 0,
    conflict_count: int = 0,
    raw_records_count: int = 0,
    unique_records_count: int = 0
) -> dict:
    start_date_obj = parse_date_to_date_obj(start_date_str)
    end_date_obj = parse_date_to_date_obj(end_date_str)
    as_of_date_obj = parse_date_to_date_obj(as_of_date_str)

    if pre_classified_txs is not None:
        classified_txs = pre_classified_txs
    else:
        # 1. Deduplicate
        unique_list, duplicate_count, conflict_count = deduplicate_contracts_control_dataset(dataset)
        raw_records_count = len(dataset)
        unique_records_count = len(unique_list)

        # 2. Classify
        classified_txs = []
        for tx in unique_list:
            res = classify_contracts_control_process(tx, as_of_date_obj, end_date_obj)
            res["tx"] = tx
            classified_txs.append(res)

    # 3. Categorize Universes
    cohort_txs = []
    operations_txs = []
    opening_backlog_txs = []
    period_started_txs = []
    period_completed_txs = []
    ending_backlog_txs = []
    excluded_data_issue_txs = []

    for c in classified_txs:
        # Cohort Universe: start_date <= start_date_obj <= end_date
        if c["start_date_obj"] and start_date_obj <= c["start_date_obj"] <= end_date_obj:
            cohort_txs.append(c)

        # Operations Universe (exclude overall data issues)
        if c["status_at_period_end"] == "data_issue" or c["current_status"] == "data_issue":
            excluded_data_issue_txs.append(c)
            continue

        is_opening = False
        is_started = False
        is_completed = False
        is_ending = False

        # Opening Backlog: start < start_date and (contract is None or contract >= start_date)
        if c["start_date_obj"] and c["start_date_obj"] < start_date_obj:
            if c["contract_date_obj"] is None or c["contract_date_obj"] >= start_date_obj:
                is_opening = True
                opening_backlog_txs.append(c)

        # Started: start between start_date and end_date
        if c["start_date_obj"] and start_date_obj <= c["start_date_obj"] <= end_date_obj:
            is_started = True
            period_started_txs.append(c)

        # Completed: contract between start_date and end_date
        if c["contract_date_obj"] and start_date_obj <= c["contract_date_obj"] <= end_date_obj:
            is_completed = True
            period_completed_txs.append(c)

        # Ending Backlog: start <= end_date and (contract is None or contract > end_date)
        if c["start_date_obj"] and c["start_date_obj"] <= end_date_obj:
            if c["contract_date_obj"] is None or c["contract_date_obj"] > end_date_obj:
                is_ending = True
                ending_backlog_txs.append(c)

        if is_opening or is_started or is_completed or is_ending:
            roles = []
            if is_opening: roles.append("opening_backlog")
            if is_started: roles.append("started_in_period")
            if is_completed: roles.append("completed_in_period")
            if is_ending: roles.append("ending_backlog")
            c["period_roles"] = roles
            operations_txs.append(c)

    # Cohort calculations
    records_count_cohort = len(cohort_txs)
    completed_cohort = [c for c in cohort_txs if c["status_at_period_end"] == "completed"]
    in_progress_cohort = [c for c in cohort_txs if c["status_at_period_end"] == "in_progress"]
    data_issue_cohort = [c for c in cohort_txs if c["status_at_period_end"] == "data_issue"]
    cancelled_cohort_count = 0

    completed_durations = [c["duration_days"] for c in completed_cohort if c["duration_days"] is not None]
    in_progress_agings = [
        c["current_aging_days"]
        for c in cohort_txs
        if c["current_status"] == "in_progress" and c["current_aging_days"] is not None
    ]

    without_manager_cohort_count = len([c for c in cohort_txs if c["tx"].get("agente_gestor") is None or str(c["tx"].get("agente_gestor")).strip() == ""])
    unknown_modality_cohort_count = len([c for c in cohort_txs if not c["tx"].get("financiamento")])

    # Operations calculations
    opening_backlog_count = len(opening_backlog_txs)
    period_started_count = len(period_started_txs)
    period_completed_count = len(period_completed_txs)
    ending_backlog_count = len(ending_backlog_txs)
    excluded_data_issue_count = len(excluded_data_issue_txs)

    # Aging and duration buckets (cohort completed/in_progress)
    duration_counts = {}
    for d in completed_durations:
        bucket = get_duration_bucket(d)
        duration_counts[bucket] = duration_counts.get(bucket, 0) + 1

    duration_labels = {
        "0_3_days": "0 a 3 dias",
        "4_7_days": "4 a 7 dias",
        "8_15_days": "8 a 15 dias",
        "16_30_days": "16 a 30 dias",
        "31_60_days": "31 a 60 dias",
        "over_60_days": "Acima de 60 dias"
    }
    duration_buckets = []
    tot_d = sum(duration_counts.values())
    for k in ["0_3_days", "4_7_days", "8_15_days", "16_30_days", "31_60_days", "over_60_days"]:
        cnt = duration_counts.get(k, 0)
        ratio = float(cnt / tot_d) if tot_d > 0 else 0.0
        duration_buckets.append({
            "key": k,
            "label": duration_labels[k],
            "count": cnt,
            "ratio": ratio
        })

    aging_counts = {}
    for a in in_progress_agings:
        bucket = get_aging_bucket(a)
        aging_counts[bucket] = aging_counts.get(bucket, 0) + 1

    aging_labels = {
        "0_3_days": "0 a 3 dias",
        "4_7_days": "4 a 7 dias",
        "8_15_days": "8 a 15 dias",
        "16_30_days": "16 a 30 dias",
        "over_30_days": "Acima de 30 dias"
    }
    aging_buckets = []
    tot_a = sum(aging_counts.values())
    for k in ["0_3_days", "4_7_days", "8_15_days", "16_30_days", "over_30_days"]:
        cnt = aging_counts.get(k, 0)
        ratio = float(cnt / tot_a) if tot_a > 0 else 0.0
        aging_buckets.append({
            "key": k,
            "label": aging_labels[k],
            "count": cnt,
            "ratio": ratio
        })

    # Operational signature SLA for processes that are still open today.
    open_sla_keys = ["under_30_days", "30_59_days", "60_89_days", "90_plus_days"]
    open_sla_labels = {
        "under_30_days": "Até 29 dias",
        "30_59_days": "30 a 59 dias",
        "60_89_days": "60 a 89 dias",
        "90_plus_days": "90 dias ou mais"
    }
    open_sla_counts = {key: 0 for key in open_sla_keys}
    for c in cohort_txs:
        if c["current_status"] != "in_progress":
            continue
        key = c.get("sla_bucket")
        if key in open_sla_counts:
            open_sla_counts[key] += 1

    total_open_sla = sum(open_sla_counts.values())
    open_sla_buckets = [
        {
            "key": key,
            "label": open_sla_labels[key],
            "count": open_sla_counts[key],
            "ratio": float(open_sla_counts[key] / total_open_sla) if total_open_sla else 0.0
        }
        for key in open_sla_keys
    ]

    # Ranking by current manually assigned responsible. Only concluded processes
    # participate in the ranking; open and unassigned volumes remain visible.
    by_responsible_dict: Dict[tuple[Optional[str], Optional[str]], list] = {}
    for c in cohort_txs:
        responsible_ref = c.get("responsible_ref")
        responsible_id = responsible_ref.get("id") if responsible_ref else None
        responsible_name = responsible_ref.get("name") if responsible_ref else None
        key = (responsible_id, responsible_name)
        by_responsible_dict.setdefault(key, []).append(c)

    by_responsible_metrics = []
    for (responsible_id, responsible_name), tx_list in by_responsible_dict.items():
        completed = [t for t in tx_list if t["status_at_period_end"] == "completed"]
        in_progress = [t for t in tx_list if t["status_at_period_end"] == "in_progress"]
        current_in_progress = [t for t in tx_list if t["current_status"] == "in_progress"]
        completed_durations_responsible = [
            t["duration_days"] for t in completed if t["duration_days"] is not None
        ]
        current_open_agings = [
            t["current_aging_days"]
            for t in tx_list
            if t["current_status"] == "in_progress" and t["current_aging_days"] is not None
        ]
        completed_count = len(completed)
        records_count = len(tx_list)
        by_responsible_metrics.append({
            "scope": "cohort",
            "rank": None,
            "responsible_id": responsible_id,
            "responsible": responsible_name,
            "ranking_eligible": responsible_id is not None and completed_count > 0,
            "ranking_basis": "average_duration_days_completed",
            "records_count": records_count,
            "completed_count": completed_count,
            "in_progress_count": len(in_progress),
            "current_in_progress_count": len(current_in_progress),
            "data_issue_count": len([
                t for t in tx_list if t["status_at_period_end"] == "data_issue"
            ]),
            "average_duration_days": calculate_average(completed_durations_responsible),
            "median_duration_days": contracts_control_calculate_median(
                completed_durations_responsible
            ),
            "p75_duration_days": contracts_control_calculate_percentile(
                completed_durations_responsible, 75.0
            ),
            "average_open_aging_days": calculate_average(current_open_agings),
            "median_open_aging_days": contracts_control_calculate_median(current_open_agings),
            "completion_ratio": float(completed_count / records_count) if records_count else 0.0,
            "unknown_modality_count": len([
                t for t in tx_list if t.get("modality") == "unknown"
            ])
        })

    ranked_responsibles = sorted(
        [metric for metric in by_responsible_metrics if metric["ranking_eligible"]],
        key=lambda metric: (
            metric["average_duration_days"],
            metric["median_duration_days"],
            -metric["completed_count"],
            (metric["responsible"] or "").casefold()
        )
    )
    for rank, metric in enumerate(ranked_responsibles, start=1):
        metric["rank"] = rank

    by_responsible_metrics.sort(
        key=lambda metric: (
            metric["rank"] is None,
            metric["rank"] or 0,
            metric["responsible"] is None,
            (metric["responsible"] or "").casefold()
        )
    )

    # Group by manager (cohort)
    by_manager_dict = {}
    for c in cohort_txs:
        mgr = c["tx"].get("agente_gestor")
        if not mgr:
            mgr = None
        if mgr not in by_manager_dict:
            by_manager_dict[mgr] = []
        by_manager_dict[mgr].append(c)

    by_manager_metrics = []
    for mgr, tx_list in by_manager_dict.items():
        recs = len(tx_list)
        comps = [t for t in tx_list if t["status_at_period_end"] == "completed"]
        comps_count = len(comps)
        in_progs = [t for t in tx_list if t["status_at_period_end"] == "in_progress"]
        in_progs_count = len(in_progs)
        issues = len([t for t in tx_list if t["status_at_period_end"] == "data_issue"])

        comp_durs = [t["duration_days"] for t in comps if t["duration_days"] is not None]
        ip_agings = [t["aging_days_at_period_end"] for t in in_progs if t["aging_days_at_period_end"] is not None]

        unk_mod_count = len([t for t in tx_list if t.get("modality") == "unknown"])

        by_manager_metrics.append({
            "scope": "cohort",
            "manager": mgr,
            "records_count": recs,
            "completed_count": comps_count,
            "in_progress_count": in_progs_count,
            "data_issue_count": issues,
            "average_duration_days": calculate_average(comp_durs),
            "median_duration_days": contracts_control_calculate_median(comp_durs),
            "p75_duration_days": contracts_control_calculate_percentile(comp_durs, 75.0),
            "average_open_aging_days": calculate_average(ip_agings),
            "median_open_aging_days": contracts_control_calculate_median(ip_agings),
            "completion_ratio": float(comps_count / recs) if recs > 0 else 0.0,
            "unknown_modality_count": unk_mod_count
        })

    # Group by modality (cohort)
    financing_count = 0
    deed_count = 0
    developer_payment_count = 0
    unknown_modality_count = 0
    modality_conflict_count = 0
    financing_amount_known_count = 0
    financing_amount_unknown_count = 0
    financing_ratio_known_count = 0

    financing_amounts_list = []
    financing_ratios_list = []

    for c in cohort_txs:
        if c["modality_confidence"] == "conflict":
            modality_conflict_count += 1

        mod = c["modality"]
        if mod == "financing":
            financing_count += 1
            if c["financing_amount"] is not None:
                financing_amount_known_count += 1
                financing_amounts_list.append(c["financing_amount"])
            else:
                financing_amount_unknown_count += 1

            if c["financing_ratio"] is not None:
                financing_ratio_known_count += 1
                financing_ratios_list.append(c["financing_ratio"])
        elif mod == "deed":
            deed_count += 1
        elif mod == "developer_payment":
            developer_payment_count += 1
        else:
            unknown_modality_count += 1

    financing_total_amount = float(sum(financing_amounts_list)) if financing_amounts_list else None
    average_financing_ratio = float(sum(financing_ratios_list) / len(financing_ratios_list)) if financing_ratios_list else None

    by_modality_data = {
        "scope": "cohort",
        "financing_count": financing_count,
        "deed_count": deed_count,
        "developer_payment_count": developer_payment_count,
        "unknown_modality_count": unknown_modality_count,
        "conflict_count": modality_conflict_count,
        "financing_amount_known_count": financing_amount_known_count,
        "financing_amount_unknown_count": financing_amount_unknown_count,
        "financing_ratio_known_count": financing_ratio_known_count,
        "financing_total_amount": financing_total_amount,
        "average_financing_ratio": average_financing_ratio
    }

    # Timeline calculations (operations)
    timeline_months = generate_months_between(start_date_obj, end_date_obj)
    timeline_metrics = []

    curr_backlog = opening_backlog_count
    for m in timeline_months:
        started_m = [c for c in operations_txs if c["start_date_obj"] and c["start_date_obj"].strftime("%Y-%m") == m]
        completed_m = [c for c in operations_txs if c["contract_date_obj"] and c["contract_date_obj"].strftime("%Y-%m") == m]

        started_cnt = len(started_m)
        completed_cnt = len(completed_m)
        net_flow = started_cnt - completed_cnt
        ending_backlog = curr_backlog + net_flow

        comp_durs = [c["duration_days"] for c in completed_m if c["duration_days"] is not None]

        issue_m_cnt = len([
            c for c in excluded_data_issue_txs
            if c["start_date_obj"] and c["start_date_obj"].strftime("%Y-%m") == m
        ])

        dt_month = datetime.strptime(m, "%Y-%m")
        month_names = {
            1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
            7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"
        }
        label = f"{month_names[dt_month.month]} {dt_month.year}"

        timeline_metrics.append({
            "month": m,
            "label": label,
            "opening_backlog": curr_backlog,
            "started_count": started_cnt,
            "completed_count": completed_cnt,
            "net_flow": net_flow,
            "ending_backlog": ending_backlog,
            "excluded_data_issue_count": issue_m_cnt,
            "average_duration_days": calculate_average(comp_durs),
            "median_duration_days": contracts_control_calculate_median(comp_durs),
            "provisional": True
        })
        curr_backlog = ending_backlog

    # Weekly operations view. Responsibility is the current manual overlay,
    # not a reconstructed historical assignment snapshot.
    weekly_timeline_metrics = []
    curr_weekly_backlog = opening_backlog_count
    for week_start, week_end in generate_contracts_control_weeks(start_date_obj, end_date_obj):
        started_week = [
            c for c in operations_txs
            if c["start_date_obj"] and week_start <= c["start_date_obj"] <= week_end
        ]
        completed_week = [
            c for c in operations_txs
            if c["contract_date_obj"] and week_start <= c["contract_date_obj"] <= week_end
        ]

        started_count = len(started_week)
        completed_count = len(completed_week)
        net_flow = started_count - completed_count
        ending_backlog = curr_weekly_backlog + net_flow

        assigned_started = [c for c in started_week if c.get("responsible_ref")]
        unassigned_started_count = started_count - len(assigned_started)
        responsible_counts: Dict[Optional[str], int] = {}
        for c in started_week:
            responsible_ref = c.get("responsible_ref")
            responsible_name = responsible_ref.get("name") if responsible_ref else None
            responsible_counts[responsible_name] = responsible_counts.get(responsible_name, 0) + 1

        by_responsible = [
            {
                "responsible": responsible_name,
                "started_count": count
            }
            for responsible_name, count in sorted(
                responsible_counts.items(),
                key=lambda item: (item[0] is None, (item[0] or "").casefold())
            )
        ]

        completed_durations_week = [
            c["duration_days"] for c in completed_week if c["duration_days"] is not None
        ]
        excluded_week_count = len([
            c for c in excluded_data_issue_txs
            if c["start_date_obj"] and week_start <= c["start_date_obj"] <= week_end
        ])

        weekly_timeline_metrics.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "label": format_contracts_control_week_label(week_start, week_end),
            "opening_backlog": curr_weekly_backlog,
            "started_count": started_count,
            "completed_count": completed_count,
            "net_flow": net_flow,
            "ending_backlog": ending_backlog,
            "assigned_started_count": len(assigned_started),
            "unassigned_started_count": unassigned_started_count,
            "assignment_completion_ratio": (
                float(len(assigned_started) / started_count) if started_count > 0 else 0.0
            ),
            "by_responsible": by_responsible,
            "excluded_data_issue_count": excluded_week_count,
            "average_duration_days": calculate_average(completed_durations_week),
            "median_duration_days": contracts_control_calculate_median(completed_durations_week),
            "provisional": True
        })
        curr_weekly_backlog = ending_backlog

    # Data Quality calculations (cohort)
    dq_records_count = len(cohort_txs)
    dq_completed = len(completed_cohort)
    dq_in_progress = len(in_progress_cohort)
    dq_valid_records_count = dq_completed + dq_in_progress

    missing_start = len([c for c in cohort_txs if not c["tx"].get("data_inicio_venda")])
    invalid_start = len([c for c in cohort_txs if c["tx"].get("data_inicio_venda") and parse_date_to_date_obj(c["tx"].get("data_inicio_venda")) is None])

    open_without_contract_date = dq_in_progress

    invalid_contract = len([
        c for c in cohort_txs
        if c["tx"].get("data_contrato") and parse_date_to_date_obj(c["tx"].get("data_contrato")) is None
    ])
    neg_duration = len([
        c for c in cohort_txs
        if c["start_date_obj"] and c["contract_date_obj"] and c["contract_date_obj"] < c["start_date_obj"]
    ])
    future_start = len([
        c for c in cohort_txs
        if c["start_date_obj"] and c["start_date_obj"] > as_of_date_obj
    ])
    missing_mgr = len([
        c for c in cohort_txs
        if c["tx"].get("agente_gestor") is None or str(c["tx"].get("agente_gestor")).strip() == ""
    ])
    missing_fin = len([
        c for c in cohort_txs
        if c["tx"].get("financiamento") is None
    ])

    mapping_status = {
        "property_title": "derived_api",
        "start_date": "resolved_api:data_inicio_venda",
        "contract_date": "resolved_api:data_contrato",
        "elapsed_days": "derived",
        "signature_sla": "derived_30_60_90",
        "responsible": "manual_bi",
        "financing_classification": "resolved_api",
        "modality_detail": "partial",
        "source_type": "resolved_api",
        "cancellation": "unresolved"
    }

    return {
        "extraction_quality": {
            "raw_records_count": raw_records_count,
            "unique_records_count": unique_records_count,
            "duplicate_transaction_count": duplicate_count,
            "duplicate_conflict_count": conflict_count,
            "duplicate_resolution_policy": "first_api_occurrence"
        },
        "cohort_summary": {
            "records_count": records_count_cohort,
            "completed_count": len(completed_cohort),
            "in_progress_count": len(in_progress_cohort),
            "data_issue_count": len(data_issue_cohort),
            "cancelled_count": cancelled_cohort_count,
            "average_duration_days": calculate_average(completed_durations),
            "median_duration_days": contracts_control_calculate_median(completed_durations),
            "p75_duration_days": contracts_control_calculate_percentile(completed_durations, 75.0),
            "p90_duration_days": contracts_control_calculate_percentile(completed_durations, 90.0),
            "average_open_aging_days": calculate_average(in_progress_agings),
            "median_open_aging_days": contracts_control_calculate_median(in_progress_agings),
            "without_manager_count": without_manager_cohort_count,
            "unknown_modality_count": unknown_modality_cohort_count
        },
        "operations_summary": {
            "opening_backlog_count": opening_backlog_count,
            "period_started_count": period_started_count,
            "period_completed_count": period_completed_count,
            "ending_backlog_count": ending_backlog_count,
            "excluded_data_issue_count": excluded_data_issue_count
        },
        "aging_buckets": aging_buckets,
        "open_sla_buckets": open_sla_buckets,
        "duration_buckets": duration_buckets,
        "by_responsible": by_responsible_metrics,
        "by_manager": by_manager_metrics,
        "by_modality": by_modality_data,
        "by_source_type": [],
        "timeline": timeline_metrics,
        "weekly_timeline": weekly_timeline_metrics,
        "data_quality": {
            "scope": "cohort",
            "records_count": dq_records_count,
            "valid_records_count": dq_valid_records_count,
            "missing_start_date_count": missing_start,
            "invalid_start_date_count": invalid_start,
            "open_without_contract_date_count": open_without_contract_date,
            "invalid_contract_date_count": invalid_contract,
            "negative_duration_count": neg_duration,
            "future_start_date_count": future_start,
            "missing_manager_count": missing_mgr,
            "missing_financing_field_count": missing_fin,
            "mapping_status": mapping_status
        },
        "cohort_txs": cohort_txs,
        "operations_txs": operations_txs
    }

# FastAPI Endpoints

@app.get(
    "/api/contracts-control/summary",
    response_model=ContractsControlSummaryResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get Contracts Control BI aggregates",
    description="Loads all transactions and returns cohort summary, operations summary, aging/duration buckets, manager metrics, modality metrics, and monthly/weekly timeline aggregates.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_contracts_control_summary(
    response: Response,
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    manager: Optional[str] = Query(None),
    responsible: Optional[str] = Query(None),
    process_status: Optional[str] = Query(None),
    modality: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    refresh: Optional[bool] = Query(None),
    db: Optional[Any] = Depends(get_db_session)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")

    sp_tz = ZoneInfo("America/Sao_Paulo")
    now_sp = datetime.now(sp_tz)
    as_of_date_str = now_sp.strftime("%Y-%m-%d")

    # 1. Resolve parameters checking both English and Portuguese query parameters
    q_params = request.query_params
    resolved_start_date = start_date or q_params.get("data_inicio")
    resolved_end_date = end_date or q_params.get("data_fim")
    resolved_manager = manager or q_params.get("gerente")
    resolved_responsible = responsible or q_params.get("responsavel")
    resolved_process_status = process_status or q_params.get("status")
    resolved_modality = modality or q_params.get("modalidade")
    resolved_source_type = source_type or q_params.get("origem")
    resolved_search = q_params.get("busca") or q_params.get("search")
    resolved_aging_bucket = q_params.get("faixa_de_tempo") or q_params.get("aging_bucket")

    if not resolved_start_date:
        resolved_start_date = f"{now_sp.year}-01-01"
    if not resolved_end_date:
        resolved_end_date = as_of_date_str

    # 2. Check endpoint cache if refresh is not requested
    cache_key = (
        "contracts-control",
        "summary",
        CONTRACTS_CONTROL_CACHE_VERSION,
        resolved_start_date,
        resolved_end_date,
        resolved_responsible,
        resolved_manager,
        resolved_process_status,
        resolved_modality,
        resolved_source_type,
        resolved_search,
        resolved_aging_bucket
    )

    import logging
    logger = logging.getLogger(__name__)

    if not refresh:
        cached_val, status = contracts_control_cache.get_status(cache_key)
        if status in ("fresh", "stale"):
            response_data, mode = cached_val
            response.headers["X-Cache"] = status
            response.headers["X-Data-Mode"] = mode
            logger.info("CC_SUMMARY_CACHE_HIT key_hash=%s cache_status=%s", hash(cache_key) % 10000, status)
            return response_data

    # 3. Load raw dataset (Miss or refresh)
    try:
        mode, src, dataset, pages_fetched, cache_status = await load_contracts_control_dataset(
            request_id=req_id, refresh=bool(refresh), caller_endpoint="/api/contracts-control/summary"
        )
    except Exception as e:
        if isinstance(e, (IntegrationUnavailableError, HTTPException, DatasetWarmingError)):
            raise e
        raise IntegrationUnavailableError(
            status_code=503,
            detail=f"Failed to load transactions: {e}",
            error_code="invalid_pipeimob_response",
            data_mode="live",
            pipeimob_connection="unavailable"
        )
    validate_dataset_origin(mode, src, dataset)

    # 4. Classify and Deduplicate
    unique_list, duplicate_count, conflict_count = deduplicate_contracts_control_dataset(dataset)
    as_of_date_obj = parse_date_to_date_obj(as_of_date_str)
    end_date_obj = parse_date_to_date_obj(resolved_end_date)

    classified_txs = []
    for tx in unique_list:
        res = classify_contracts_control_process(tx, as_of_date_obj, end_date_obj)
        res["tx"] = tx
        classified_txs.append(res)

    tx_ids = [
        str(c["tx"].get("transacao_unique_id_pipeimob"))
        for c in classified_txs
        if c["tx"].get("transacao_unique_id_pipeimob")
    ]

    # 5. Load and Apply Manual Overlay
    from services.contracts_control_manual_service import ContractsControlManualService
    manual_overlay = {}
    if db and tx_ids:
        try:
            manual_overlay = ContractsControlManualService.get_manual_data_for_overlay(db, tx_ids)
        except Exception as e:
            logger.error(f"Manual data layer overlay query failed: {type(e).__name__}")
            manual_overlay = {}

    for c in classified_txs:
        tx = c["tx"]
        tx_id = str(tx.get("transacao_unique_id_pipeimob")) if tx.get("transacao_unique_id_pipeimob") is not None else None
        responsible_ref = None
        manual_version = 0
        if tx_id and tx_id in manual_overlay:
            responsible_ref = manual_overlay[tx_id]["responsible"]
            manual_version = manual_overlay[tx_id].get("version", 0)
        c["responsible_ref"] = responsible_ref
        c["manual_data_version"] = manual_version

    # 6. Apply Filters to classified_txs
    from models.contracts_control import normalize_responsible_name
    filtered_classified_txs = []
    for c in classified_txs:
        tx = c["tx"]

        if resolved_manager and tx.get("agente_gestor") != resolved_manager:
            continue

        if resolved_process_status and c["status_at_period_end"] != resolved_process_status:
            continue

        tx_modality = c["modality"]
        if resolved_modality and tx_modality != resolved_modality:
            continue

        if resolved_aging_bucket and not matches_contracts_control_aging_filter(
            c, resolved_aging_bucket
        ):
            continue

        if resolved_search:
            search_lower = resolved_search.lower()
            prop_code = tx.get("codigo_imovel") or ""
            mgr = tx.get("agente_gestor") or ""
            if search_lower not in prop_code.lower() and search_lower not in mgr.lower():
                continue

        if resolved_responsible:
            responsible_ref = c["responsible_ref"]
            if not responsible_ref:
                continue
            norm_resp_filter = normalize_responsible_name(resolved_responsible)
            norm_resp_name = normalize_responsible_name(responsible_ref["name"])
            if responsible_ref["id"] != resolved_responsible and norm_resp_name != norm_resp_filter:
                continue

        filtered_classified_txs.append(c)

    # 7. Compute Aggregates on filtered list
    try:
        aggregates = compute_contracts_control_data(
            dataset,
            resolved_start_date,
            resolved_end_date,
            as_of_date_str,
            pre_classified_txs=filtered_classified_txs,
            duplicate_count=duplicate_count,
            conflict_count=conflict_count,
            raw_records_count=len(dataset),
            unique_records_count=len(unique_list)
        )
    except Exception as e:
        raise IntegrationUnavailableError(
            status_code=503,
            detail=f"Failed to compute contracts control aggregates: {e}",
            error_code="aggregation_failed",
            data_mode=mode,
            pipeimob_connection="internal_error"
        )

    # 8. Set Headers
    response.headers["X-Data-Mode"] = mode
    response.headers["X-Cache"] = cache_status

    resp_period = ContractsControlPeriod(
        start=resolved_start_date,
        end=resolved_end_date,
        basis="data_inicio_venda",
        as_of_date=as_of_date_str
    )

    resp_extraction = ContractsControlExtraction(
        upstream_endpoint="/api/v2/negocios/transacoes",
        upstream_filter_field="data_inicio_criacao",
        coverage_start="2020-01-01",
        coverage_end=as_of_date_str,
        pages_fetched=pages_fetched,
        raw_records_fetched=len(dataset),
        coverage_status="unverified"
    )

    operations_tx_ids = list({
        str(c["tx"].get("transacao_unique_id_pipeimob"))
        for c in aggregates["operations_txs"]
        if c["tx"].get("transacao_unique_id_pipeimob")
    })

    enrichment_data = {
        "status": "available",
        "scope": "operations",
        "eligible_records_count": len(operations_tx_ids),
        "responsible_filled_count": 0,
        "responsible_pending_count": len(operations_tx_ids),
        "responsible_completion_ratio": 0.0,
        "last_manual_update_at": None
    }

    if db:
        try:
            raw_indicators = ContractsControlManualService.get_enrichment_indicators(db, operations_tx_ids)
            enrichment_data.update(raw_indicators)
            enrichment_data["status"] = "available"
        except Exception as e:
            logger.error(f"Manual data layer indicators query failed: {type(e).__name__}")
            enrichment_data = {
                "status": "unavailable",
                "scope": "operations",
                "eligible_records_count": None,
                "responsible_filled_count": None,
                "responsible_pending_count": None,
                "responsible_completion_ratio": None,
                "last_manual_update_at": None
            }
    else:
        enrichment_data = {
            "status": "unavailable",
            "scope": "operations",
            "eligible_records_count": None,
            "responsible_filled_count": None,
            "responsible_pending_count": None,
            "responsible_completion_ratio": None,
            "last_manual_update_at": None
        }

    response_data = ContractsControlSummaryResponse(
        period=resp_period,
        extraction=resp_extraction,
        extraction_quality=ContractsControlExtractionQuality(**aggregates["extraction_quality"]),
        cohort_summary=ContractsControlCohortSummary(**aggregates["cohort_summary"]),
        operations_summary=ContractsControlOperationsSummary(**aggregates["operations_summary"]),
        aging_buckets=[ContractsControlBucket(**b) for b in aggregates["aging_buckets"]],
        open_sla_buckets=[ContractsControlBucket(**b) for b in aggregates["open_sla_buckets"]],
        duration_buckets=[ContractsControlBucket(**b) for b in aggregates["duration_buckets"]],
        by_responsible=[
            ContractsControlResponsibleMetric(**metric)
            for metric in aggregates["by_responsible"]
        ],
        by_manager=[ContractsControlManagerMetric(**m) for m in aggregates["by_manager"]],
        by_modality=ContractsControlModalitySummary(**aggregates["by_modality"]),
        by_source_type=[],
        timeline=[ContractsControlTimelineMetric(**t) for t in aggregates["timeline"]],
        weekly_timeline=[
            ContractsControlWeeklyTimelineMetric(**t) for t in aggregates["weekly_timeline"]
        ],
        data_quality=ContractsControlDataQuality(**aggregates["data_quality"]),
        manual_enrichment=ContractsControlManualEnrichment(**enrichment_data)
    )

    contracts_control_cache.set(cache_key, (response_data, mode))

    logger.info("CC_SUMMARY_PROCESSING_DONE key_hash=%s cache_status=%s raw_count=%s filtered_count=%s eligible_records=%s",
                hash(cache_key) % 10000, cache_status, len(dataset), len(filtered_classified_txs), len(operations_tx_ids))

    return response_data

@app.get(
    "/api/contracts-control/deals",
    response_model=ContractsControlDealsResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get paginated list of Contracts Control deals",
    description="Returns a paginated list of operational deals matching filters. PII is strictly stripped.",
    dependencies=[Depends(verify_backend_api_key)]
)
async def get_contracts_control_deals(
    response: Response,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    scope: str = Query("operations"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    manager: Optional[str] = Query(None),
    responsible: Optional[str] = Query(None),
    process_status: Optional[str] = Query(None),
    modality: Optional[str] = Query(None),
    source_type: Optional[str] = Query(None),
    aging_bucket: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    refresh: Optional[bool] = Query(None),
    db: Optional[Any] = Depends(get_db_session)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")

    sp_tz = ZoneInfo("America/Sao_Paulo")
    now_sp = datetime.now(sp_tz)
    as_of_date_str = now_sp.strftime("%Y-%m-%d")

    # 1. Resolve parameters checking both English and Portuguese query parameters
    q_params = request.query_params
    resolved_start_date = start_date or q_params.get("data_inicio")
    resolved_end_date = end_date or q_params.get("data_fim")
    resolved_manager = manager or q_params.get("gerente")
    resolved_responsible = responsible or q_params.get("responsavel")
    resolved_process_status = process_status or q_params.get("status")
    resolved_modality = modality or q_params.get("modalidade")
    resolved_source_type = source_type or q_params.get("origem")
    resolved_search = search or q_params.get("busca")
    resolved_aging_bucket = aging_bucket or q_params.get("faixa_de_tempo")

    if not resolved_start_date:
        resolved_start_date = f"{now_sp.year}-01-01"
    if not resolved_end_date:
        resolved_end_date = as_of_date_str

    # 2. Check endpoint cache if refresh is not requested
    cache_key = (
        "contracts-control",
        "deals",
        CONTRACTS_CONTROL_CACHE_VERSION,
        resolved_start_date,
        resolved_end_date,
        scope,
        resolved_responsible,
        resolved_manager,
        resolved_process_status,
        resolved_modality,
        resolved_source_type,
        resolved_search,
        resolved_aging_bucket,
        page,
        page_size
    )

    import logging
    logger = logging.getLogger(__name__)

    if not refresh:
        cached_val, status = contracts_control_cache.get_status(cache_key)
        if status in ("fresh", "stale"):
            response_data, mode = cached_val
            response.headers["X-Cache"] = status
            response.headers["X-Data-Mode"] = mode
            logger.info("CC_DEALS_CACHE_HIT key_hash=%s cache_status=%s", hash(cache_key) % 10000, status)
            return response_data

    # 3. Load dataset (Miss or refresh)
    mode, src, dataset, pages_fetched, cache_status = await load_contracts_control_dataset(
        request_id=req_id, refresh=bool(refresh), caller_endpoint="/api/contracts-control/deals"
    )
    validate_dataset_origin(mode, src, dataset)

    # 4. Process and Filter
    aggregates = compute_contracts_control_data(dataset, resolved_start_date, resolved_end_date, as_of_date_str)

    if scope == "cohort":
        deals_source = aggregates["cohort_txs"]
    else:
        deals_source = aggregates["operations_txs"]

    tx_ids = [
        str(c["tx"].get("transacao_unique_id_pipeimob"))
        for c in deals_source
        if c["tx"].get("transacao_unique_id_pipeimob")
    ]

    from services.contracts_control_manual_service import ContractsControlManualService
    manual_overlay = {}
    if db and tx_ids:
        try:
            manual_overlay = ContractsControlManualService.get_manual_data_for_overlay(db, tx_ids)
        except Exception as e:
            logger.error(f"Manual data layer overlay query failed: {type(e).__name__}")
            manual_overlay = {}

    from models.contracts_control import normalize_responsible_name

    filtered_deals = []
    for c in deals_source:
        tx = c["tx"]

        if resolved_manager and tx.get("agente_gestor") != resolved_manager:
            continue

        if resolved_process_status and c["status_at_period_end"] != resolved_process_status:
            continue

        tx_modality = c["modality"]
        if resolved_modality and tx_modality != resolved_modality:
            continue

        if resolved_aging_bucket and not matches_contracts_control_aging_filter(
            c, resolved_aging_bucket
        ):
            continue

        if resolved_search:
            search_lower = resolved_search.lower()
            prop_code = tx.get("codigo_imovel") or ""
            mgr = tx.get("agente_gestor") or ""
            if search_lower not in prop_code.lower() and search_lower not in mgr.lower():
                continue

        tx_id = str(tx.get("transacao_unique_id_pipeimob")) if tx.get("transacao_unique_id_pipeimob") is not None else None
        responsible_ref = None
        manual_version = 0
        if tx_id and tx_id in manual_overlay:
            responsible_ref = manual_overlay[tx_id]["responsible"]
            manual_version = manual_overlay[tx_id].get("version", 0)

        if resolved_responsible:
            if not responsible_ref:
                continue
            norm_resp_filter = normalize_responsible_name(resolved_responsible)
            norm_resp_name = normalize_responsible_name(responsible_ref["name"])
            if responsible_ref["id"] != resolved_responsible and norm_resp_name != norm_resp_filter:
                continue

        property_title, property_title_source = build_contracts_control_property_title(tx)

        deal_item = {
            "transaction_id": tx_id,
            "property_code": tx.get("codigo_imovel") or "",
            "property_title": property_title,
            "property_title_source": property_title_source,
            "start_date": tx.get("data_inicio_venda"),
            "contract_date": tx.get("data_contrato"),
            "duration_days": c["duration_days"],
            "current_aging_days": c["current_aging_days"],
            "aging_days_at_period_end": c["aging_days_at_period_end"],
            "elapsed_days": c["elapsed_days"],
            "sla_bucket": c["sla_bucket"],
            "sla_label": c["sla_label"],
            "sla_action": c["sla_action"],
            "manager": tx.get("agente_gestor"),
            "responsible": responsible_ref,
            "manual_data_version": manual_version,
            "modality": c["modality"],
            "modality_label": c["modality_label"],
            "modality_source": c["modality_source"],
            "modality_confidence": c["modality_confidence"],
            "financing_bank": c["financing_bank"],
            "financing_amount": c["financing_amount"],
            "financing_ratio": c["financing_ratio"],
            "modality_flags": c["modality_flags"],
            "source_type": c["source_type"],
            "source_type_label": c["source_type_label"],
            "current_status": c["current_status"],
            "status_at_period_end": c["status_at_period_end"],
            "data_quality_flags": c["data_quality_flags"]
        }
        if scope == "operations":
            deal_item["period_roles"] = c.get("period_roles", [])

        filtered_deals.append(deal_item)

    total_records = len(filtered_deals)
    total_pages = max(1, math.ceil(total_records / page_size))

    if page > total_pages:
        paginated_deals = []
    else:
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_deals = filtered_deals[start_idx:end_idx]

    response_data = ContractsControlDealsResponse(
        page=page,
        page_size=page_size,
        total_records=total_records,
        total_pages=total_pages,
        deals=[ContractsControlDeal(**d) for d in paginated_deals]
    )

    contracts_control_cache.set(cache_key, (response_data, mode))
    response.headers["X-Data-Mode"] = mode
    response.headers["X-Cache"] = cache_status

    logger.info("CC_DEALS_PROCESSING_DONE key_hash=%s cache_status=%s raw_count=%s filtered_count=%s paginated_count=%s",
                hash(cache_key) % 10000, cache_status, len(deals_source), total_records, len(paginated_deals))

    return response_data

@app.get(
    "/api/contracts-control/responsibles",
    response_model=ContractsControlResponsiblesResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get list of responsibles"
)
async def get_contracts_control_responsibles(
    include_inactive: bool = Query(False),
    payload: dict = Depends(verify_backend_api_key),
    db: Optional[Any] = Depends(get_db_session)
):
    if include_inactive:
        await require_contracts_control_temporary_admin(payload)

    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    from services.contracts_control_manual_service import ContractsControlManualService
    resps = ContractsControlManualService.list_responsibles(db, include_inactive)
    return ContractsControlResponsiblesResponse(
        responsibles=[
            ContractsControlResponsibleReference(
                id=str(r.id),
                name=r.name,
                active=r.active
            ) for r in resps
        ]
    )

@app.post(
    "/api/contracts-control/responsibles",
    response_model=ContractsControlResponsibleReference,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Create a new responsible"
)
async def create_contracts_control_responsible(
    req: CreateResponsibleRequest,
    sub: str = Depends(require_contracts_control_temporary_admin),
    db: Optional[Any] = Depends(get_db_session)
):
    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    from services.contracts_control_manual_service import ContractsControlManualService
    try:
        resp = ContractsControlManualService.create_responsible(db, req.name)
        db.commit()
        return ContractsControlResponsibleReference(
            id=str(resp.id),
            name=resp.name,
            active=resp.active
        )
    except ValueError as e:
        err_str = str(e)
        if err_str == "empty_name":
            raise HTTPException(status_code=422, detail="Name cannot be empty.")
        elif err_str == "duplicate_name":
            raise HTTPException(status_code=409, detail="A responsible with this name already exists.")
        raise HTTPException(status_code=400, detail=err_str)

@app.patch(
    "/api/contracts-control/responsibles/{responsible_id}",
    response_model=ContractsControlResponsibleReference,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Update a responsible"
)
async def update_contracts_control_responsible(
    responsible_id: str,
    req: UpdateResponsibleRequest,
    sub: str = Depends(require_contracts_control_temporary_admin),
    db: Optional[Any] = Depends(get_db_session)
):
    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    try:
        resp_uuid = uuid.UUID(responsible_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Responsible not found.")

    from services.contracts_control_manual_service import ContractsControlManualService
    try:
        resp = ContractsControlManualService.update_responsible(
            db, resp_uuid, req.name, req.active
        )
        db.commit()
        return ContractsControlResponsibleReference(
            id=str(resp.id),
            name=resp.name,
            active=resp.active
        )
    except ValueError as e:
        err_str = str(e)
        if err_str == "responsible_not_found":
            raise HTTPException(status_code=404, detail="Responsible not found.")
        elif err_str == "empty_name":
            raise HTTPException(status_code=422, detail="Name cannot be empty.")
        elif err_str == "duplicate_name":
            raise HTTPException(status_code=409, detail="A responsible with this name already exists.")
        raise HTTPException(status_code=400, detail=err_str)

@app.patch(
    "/api/contracts-control/deals/{transaction_id}/manual-data",
    response_model=IndividualAttributionResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Set or update responsible for a transaction"
)
async def patch_transaction_manual_data(
    transaction_id: str,
    req: UpdateIndividualAttributionRequest,
    sub: str = Depends(require_contracts_control_temporary_admin),
    db: Optional[Any] = Depends(get_db_session)
):
    import time
    import logging
    logger = logging.getLogger(__name__)

    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    # Stage 1: load_dataset
    t_start = time.perf_counter()
    logger.info("CC_PATCH_STAGE_START stage=load_dataset")
    try:
        dataset = await get_contracts_control_dataset_for_write()
        valid_ids = set()
        for c in dataset:
            if isinstance(c, dict):
                tx_id = c["tx"].get("transacao_unique_id_pipeimob") if "tx" in c and isinstance(c["tx"], dict) else c.get("transacao_unique_id_pipeimob")
                if tx_id:
                    valid_ids.add(tx_id)

        duration = int((time.perf_counter() - t_start) * 1000)
        logger.info(f"CC_PATCH_STAGE_END stage=load_dataset duration_ms={duration}")
    except Exception as e:
        duration = int((time.perf_counter() - t_start) * 1000)
        logger.error(f"CC_PATCH_STAGE_ERROR stage=load_dataset exception={type(e).__name__} duration_ms={duration}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=503, detail="Pipeimob dataset universe is unavailable.")

    if transaction_id not in valid_ids:
        raise HTTPException(status_code=404, detail="Transaction not found in Pipeimob dataset.")

    resp_uuid = None
    if req.responsible_id is not None:
        try:
            resp_uuid = uuid.UUID(req.responsible_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Responsible not found.")

    # Stage 2: db_operation
    t_db_start = time.perf_counter()
    logger.info("CC_PATCH_STAGE_START stage=db_operation")
    from services.contracts_control_manual_service import ContractsControlManualService
    try:
        md, changed = ContractsControlManualService.update_individual_attribution(
            db, transaction_id, resp_uuid, req.version, sub
        )
        db.commit()
        contracts_control_cache.clear_endpoint_caches()

        resp_ref = None
        if md.responsible:
            resp_ref = ContractsControlResponsibleReference(
                id=str(md.responsible.id),
                name=md.responsible.name,
                active=md.responsible.active
            )

        duration = int((time.perf_counter() - t_db_start) * 1000)
        logger.info(f"CC_PATCH_STAGE_END stage=db_operation duration_ms={duration}")

        return IndividualAttributionResponse(
            transaction_id=md.transaction_id,
            responsible=resp_ref,
            version=md.version,
            updated_at=md.updated_at.isoformat(),
            changed=changed
        )
    except Exception as e:
        db.rollback()
        duration = int((time.perf_counter() - t_db_start) * 1000)
        logger.error(f"CC_PATCH_STAGE_ERROR stage=db_operation exception={type(e).__name__} duration_ms={duration}")
        if isinstance(e, ValueError):
            err_str = str(e)
            if err_str == "responsible_not_found":
                raise HTTPException(status_code=404, detail="Responsible not found.")
            elif err_str == "responsible_inactive":
                raise HTTPException(status_code=422, detail="Cannot assign an inactive responsible.")
            elif err_str == "version_conflict":
                raise HTTPException(status_code=409, detail="Version conflict. Optimistic locking check failed.")
            raise HTTPException(status_code=400, detail=err_str)
        raise e

@app.post(
    "/api/contracts-control/manual-data/bulk",
    response_model=BulkAttributionResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Bulk set or update responsible for transactions"
)
async def post_bulk_manual_data(
    req: BulkAttributionRequest,
    sub: str = Depends(require_contracts_control_temporary_admin),
    db: Optional[Any] = Depends(get_db_session)
):
    import time
    import logging
    logger = logging.getLogger(__name__)

    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    if not req.items:
        raise HTTPException(status_code=422, detail="Items list cannot be empty.")
    if len(req.items) > 100:
        raise HTTPException(status_code=422, detail="Lote excede o limite máximo de 100 itens.")

    tx_ids = [item.transaction_id for item in req.items]
    if len(tx_ids) != len(set(tx_ids)):
        raise HTTPException(status_code=422, detail="Lote contém IDs de transação duplicados.")

    # Stage 1: load_dataset
    t_start = time.perf_counter()
    logger.info("CC_PATCH_STAGE_START stage=load_dataset")
    try:
        dataset = await get_contracts_control_dataset_for_write()
        valid_ids = set()
        for c in dataset:
            if isinstance(c, dict):
                tx_id = c["tx"].get("transacao_unique_id_pipeimob") if "tx" in c and isinstance(c["tx"], dict) else c.get("transacao_unique_id_pipeimob")
                if tx_id:
                    valid_ids.add(tx_id)

        duration = int((time.perf_counter() - t_start) * 1000)
        logger.info(f"CC_PATCH_STAGE_END stage=load_dataset duration_ms={duration}")
    except Exception as e:
        duration = int((time.perf_counter() - t_start) * 1000)
        logger.error(f"CC_PATCH_STAGE_ERROR stage=load_dataset exception={type(e).__name__} duration_ms={duration}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=503, detail="Pipeimob dataset universe is unavailable.")

    resp_uuid = None
    if req.responsible_id is not None:
        try:
            resp_uuid = uuid.UUID(req.responsible_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Responsible not found.")

    items_dicts = [{"transaction_id": item.transaction_id, "version": item.version} for item in req.items]

    # Stage 2: db_operation
    t_db_start = time.perf_counter()
    logger.info("CC_PATCH_STAGE_START stage=db_operation")
    from services.contracts_control_manual_service import ContractsControlManualService
    try:
        res = ContractsControlManualService.update_bulk_attribution(
            db, items_dicts, resp_uuid, sub, valid_ids
        )
        contracts_control_cache.clear_endpoint_caches()
        duration = int((time.perf_counter() - t_db_start) * 1000)
        logger.info(f"CC_PATCH_STAGE_END stage=db_operation duration_ms={duration}")

        return BulkAttributionResponse(
            requested_count=res["requested_count"],
            updated_count=res["updated_count"],
            unchanged_count=res["unchanged_count"],
            items=[BulkAttributionItemResponse(**it) for it in res["items"]]
        )
    except Exception as e:
        db.rollback()
        duration = int((time.perf_counter() - t_db_start) * 1000)
        logger.error(f"CC_PATCH_STAGE_ERROR stage=db_operation exception={type(e).__name__} duration_ms={duration}")
        if isinstance(e, ValueError):
            err_str = str(e)
            if err_str.startswith("transaction_not_found:"):
                invalid_tx = err_str.split(":")[1]
                raise HTTPException(status_code=404, detail=f"Transaction {invalid_tx} not found.")
            elif err_str == "responsible_not_found":
                raise HTTPException(status_code=404, detail="Responsible not found.")
            elif err_str == "responsible_inactive":
                raise HTTPException(status_code=422, detail="Cannot assign an inactive responsible.")
            elif err_str == "version_conflict":
                raise HTTPException(status_code=409, detail="Version conflict. Optimistic locking check failed.")
            elif err_str == "items_empty":
                raise HTTPException(status_code=422, detail="Items list cannot be empty.")
            elif err_str == "items_limit_exceeded":
                raise HTTPException(status_code=422, detail="Lote excede o limite máximo de 100 itens.")
            elif err_str == "empty_transaction_id":
                raise HTTPException(status_code=422, detail="Lote contém IDs de transação vazios.")
            elif err_str == "duplicate_transaction_ids":
                raise HTTPException(status_code=422, detail="Lote contém IDs de transação duplicados.")
            raise HTTPException(status_code=400, detail=err_str)
        raise e

@app.get(
    "/api/contracts-control/deals/{transaction_id}/manual-data/history",
    response_model=List[HistoryRecordItem],
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get transaction manual data history",
    dependencies=[Depends(require_contracts_control_temporary_admin)]
)
async def get_transaction_manual_data_history(
    transaction_id: str,
    db: Optional[Any] = Depends(get_db_session)
):
    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    from repositories.contracts_control_repository import ContractsControlRepository
    history_records = ContractsControlRepository.get_history_by_transaction_id(db, transaction_id)

    result = []
    for rec in history_records:
        prev_resp = None
        new_resp = None
        if rec.field_name == "responsible_id":
            if rec.previous_value:
                try:
                    prev_id = uuid.UUID(rec.previous_value)
                    r = ContractsControlRepository.get_responsible_by_id(db, prev_id)
                    if r:
                        prev_resp = HistoryResponsibleReference(
                            id=str(r.id), current_name=r.name, active=r.active
                        )
                except ValueError:
                    pass
            if rec.new_value:
                try:
                    new_id = uuid.UUID(rec.new_value)
                    r = ContractsControlRepository.get_responsible_by_id(db, new_id)
                    if r:
                        new_resp = HistoryResponsibleReference(
                            id=str(r.id), current_name=r.name, active=r.active
                        )
                except ValueError:
                    pass
        result.append(
            HistoryRecordItem(
                field_name=rec.field_name,
                previous_responsible=prev_resp,
                new_responsible=new_resp,
                previous_version=rec.previous_version,
                new_version=rec.new_version,
                changed_at=rec.changed_at.isoformat(),
                changed_by_sub=rec.changed_by_sub
            )
        )

    return result

# Spreadsheet Import Preview Pydantic Schemas
class ImportPreviewSummaryResponse(BaseModel):
    source_rows_count: int
    unique_property_codes_count: int
    rows_with_responsible_count: int
    rows_without_responsible_count: int
    duplicate_same_value_count: int
    duplicate_conflict_count: int
    unique_match_count: int
    ambiguous_match_count: int
    not_found_count: int
    responsible_not_registered_count: int
    already_synchronized_count: int
    to_assign_count: int
    to_change_count: int
    to_clear_count: int
    invalid_source_row_count: int = 0

class ImportPreviewItemResponse(BaseModel):
    id: str
    aba: str
    linha: int
    codigo_imovel: Optional[str] = None
    nome_imovel: Optional[str] = None
    responsavel_planilha: Optional[str] = None
    responsavel_atual_secretaria: Optional[str] = None
    transaction_id: Optional[str] = None
    versao_manual_atual: Optional[int] = None
    decisao_proposta: str
    motivo: Optional[str] = None
    source_occurrences: Optional[dict] = None

class ImportPreviewResponse(BaseModel):
    preview_id: str
    source_filename: str
    source_format: str
    parser_version: str
    created_by_sub: str
    status: str
    source_hash: str
    created_at: str
    expires_at: str
    summary: ImportPreviewSummaryResponse
    page: int
    page_size: int
    total_records: int
    total_pages: int
    items: List[ImportPreviewItemResponse]

@app.post(
    "/api/contracts-control/imports/responsibles/preview",
    response_model=ImportPreviewResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Generate a responsible spreadsheet import preview"
)
async def post_import_responsibles_preview(
    file: UploadFile = File(...),
    sub: str = Depends(require_contracts_control_temporary_admin),
    db: Optional[Any] = Depends(get_db_session)
):
    import os
    import tempfile
    from services.contracts_control_import_service import ContractsControlImportService
    from repositories.contracts_control_import_repository import ContractsControlImportRepository

    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    # Validate file extension
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".xlsx", ".csv"):
        raise HTTPException(status_code=400, detail="Invalid file format. Only .xlsx and .csv are supported.")

    # Save to a temporary file, ensuring it gets removed in finally
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name

        # Fetch Pipeimob dataset
        try:
            dataset = await get_contracts_control_dataset_for_write()
        except HTTPException as e:
            if e.status_code == 503 and "cache is empty" in str(e.detail):
                _, _, dataset, _, _ = await load_contracts_control_dataset(
                    request_id=sub, caller_endpoint="/api/contracts-control/imports/responsibles/preview"
                )
            else:
                raise e

        # Generate preview
        preview = await ContractsControlImportService.create_import_preview(
            db=db,
            file_path=tmp_path,
            filename=file.filename,
            created_by_sub=sub,
            dataset=dataset
        )
        db.commit()

        # Retrieve first page of preview items (page=1, page_size=25)
        items, total_count = ContractsControlImportRepository.get_preview_items_paginated(
            db=db,
            preview_id=preview.id,
            page=1,
            page_size=25,
            filters={}
        )

        total_pages = (total_count + 24) // 25

        # Build response items
        resp_items = []
        for it in items:
            resp_items.append(ImportPreviewItemResponse(
                id=str(it.id),
                aba=it.aba,
                linha=it.linha,
                codigo_imovel=it.codigo_imovel,
                nome_imovel=it.nome_imovel,
                responsavel_planilha=it.responsavel_planilha,
                responsavel_atual_secretaria=it.responsavel_atual_secretaria,
                transaction_id=it.transaction_id,
                versao_manual_atual=it.versao_manual_atual,
                decisao_proposta=it.decisao_proposta,
                motivo=it.motivo,
                source_occurrences=it.source_occurrences
            ))

        return ImportPreviewResponse(
            preview_id=str(preview.id),
            source_filename=preview.source_filename,
            source_format=preview.source_format,
            parser_version=preview.parser_version,
            created_by_sub=preview.created_by_sub,
            status=preview.status,
            source_hash=preview.source_hash,
            created_at=preview.created_at.isoformat(),
            expires_at=preview.expires_at.isoformat(),
            summary=ImportPreviewSummaryResponse(**preview.summary),
            page=1,
            page_size=25,
            total_records=total_count,
            total_pages=total_pages,
            items=resp_items
        )
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as e:
        db.rollback()
        if isinstance(e, (HTTPException, InvalidSpreadsheetError, DatasetWarmingError)):
            raise e
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except:
                pass

@app.get(
    "/api/contracts-control/imports/responsibles/previews/{preview_id}",
    response_model=ImportPreviewResponse,
    responses={**RESPONSES_503, **RESPONSES_AUTH},
    summary="Get details of a spreadsheet import preview"
)
async def get_import_responsibles_preview(
    preview_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: Optional[str] = Query(None),
    responsavel: Optional[str] = Query(None),
    codigo: Optional[str] = Query(None),
    aba: Optional[str] = Query(None),
    only_pending: bool = Query(False),
    sub: str = Depends(require_contracts_control_temporary_admin),
    db: Optional[Any] = Depends(get_db_session)
):
    from repositories.contracts_control_import_repository import ContractsControlImportRepository

    if not db:
        raise HTTPException(status_code=503, detail="Database session unavailable.")

    try:
        p_uuid = uuid.UUID(preview_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Preview not found.")

    preview = ContractsControlImportRepository.get_preview_by_id(db, p_uuid)
    if not preview:
        raise HTTPException(status_code=404, detail="Preview not found.")

    # Check expiry -> Return HTTP 410 Gone
    now_dt = datetime.now(timezone.utc)
    expires_at = preview.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now_dt:
        raise HTTPException(status_code=410, detail="Preview has expired.")

    filters = {
        "status": status,
        "responsavel": responsavel,
        "codigo": codigo,
        "aba": aba,
        "only_pending": only_pending
    }

    items, total_count = ContractsControlImportRepository.get_preview_items_paginated(
        db=db,
        preview_id=p_uuid,
        page=page,
        page_size=page_size,
        filters=filters
    )

    total_pages = (total_count + page_size - 1) // page_size

    resp_items = []
    for it in items:
        resp_items.append(ImportPreviewItemResponse(
            id=str(it.id),
            aba=it.aba,
            linha=it.linha,
            codigo_imovel=it.codigo_imovel,
            nome_imovel=it.nome_imovel,
            responsavel_planilha=it.responsavel_planilha,
            responsavel_atual_secretaria=it.responsavel_atual_secretaria,
            transaction_id=it.transaction_id,
            versao_manual_atual=it.versao_manual_atual,
            decisao_proposta=it.decisao_proposta,
            motivo=it.motivo,
            source_occurrences=it.source_occurrences
        ))

    return ImportPreviewResponse(
        preview_id=str(preview.id),
        source_filename=preview.source_filename,
        source_format=preview.source_format,
        parser_version=preview.parser_version,
        created_by_sub=preview.created_by_sub,
        status=preview.status,
        source_hash=preview.source_hash,
        created_at=preview.created_at.isoformat(),
        expires_at=preview.expires_at.isoformat(),
        summary=ImportPreviewSummaryResponse(**preview.summary),
        page=page,
        page_size=page_size,
        total_records=total_count,
        total_pages=total_pages,
        items=resp_items
    )
