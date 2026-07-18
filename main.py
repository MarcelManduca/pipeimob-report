import os
import urllib.request
import json
import ssl
import time
from datetime import datetime, timezone
from typing import List, Optional, Union
from decimal import Decimal
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Header, Query, HTTPException, Response, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Token Cache in memory
class TokenCache:
    def __init__(self):
        self.access_token: Optional[str] = None
        self.token_type: str = "Bearer"
        self.expires_at: Optional[float] = None

token_cache = TokenCache()

# Dashboard Cache in memory (5 min TTL)
class DashboardCache:
    def __init__(self):
        from threading import Lock
        self.cache = {}
        self.lock = Lock()
        
    def get(self, key):
        with self.lock:
            if key in self.cache:
                val, expires_at = self.cache[key]
                if time.time() < expires_at:
                    return val
                else:
                    del self.cache[key]
            return None
            
    def set(self, key, val, ttl=300):
        with self.lock:
            self.cache[key] = (val, time.time() + ttl)

    def clear(self):
        with self.lock:
            self.cache.clear()

dashboard_cache = DashboardCache()
dashboard_cache.clear()

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
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
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
        with urllib.request.urlopen(req, context=ssl_context, timeout=8) as response:
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
            detail="Pipeimob CRM API request timed out." if is_timeout else "Pipeimob CRM API request is unreachable.",
            error_code="pipeimob_timeout" if is_timeout else "pipeimob_unavailable",
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
    
    def request_with_retry(url: str, retry_allowed: bool = True) -> dict:
        nonlocal token
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {token}', 'User-Agent': 'Mozilla/5.0'}
        )
        try:
            with urllib.request.urlopen(req, context=ssl_context, timeout=12) as response:
                res_body = json.loads(response.read().decode('utf-8'))
                if not res_body.get("success"):
                    raise IntegrationUnavailableError(
                        status_code=503,
                        detail="Pipeimob transactions API returned success=False.",
                        error_code="invalid_pipeimob_response",
                        data_mode="live",
                        pipeimob_connection="unavailable"
                    )
                return res_body
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_allowed:
                token = get_auth_token(api_key, api_secret, force_refresh=True)
                return request_with_retry(url, retry_allowed=False)
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
                detail="Pipeimob CRM API request timed out." if is_timeout else "Pipeimob CRM API request is unreachable.",
                error_code="pipeimob_timeout" if is_timeout else "pipeimob_unavailable",
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
        
        txs = res_body.get("data", {}).get("transacoes", []) if isinstance(res_body.get("data"), dict) else []
        
        for tx in txs:
            tx_id = tx.get("transacao_unique_id_pipeimob")
            if tx_id:
                if tx_id not in seen_ids:
                    seen_ids.add(tx_id)
                    all_transactions.append(tx)
            else:
                all_transactions.append(tx)
                
        meta_p = None
        if "meta" in res_body and isinstance(res_body["meta"], dict) and "pagination" in res_body["meta"]:
            meta_p = res_body["meta"]["pagination"]
        elif "data" in res_body and isinstance(res_body["data"], dict) and "meta" in res_body["data"] and isinstance(res_body["data"]["meta"], dict) and "pagination" in res_body["data"]["meta"]:
            meta_p = res_body["data"]["meta"]["pagination"]
            
        if meta_p is None:
            raise IntegrationUnavailableError(
                status_code=503,
                detail="Pagination metadata not found in Pipeimob response.",
                error_code="invalid_pipeimob_response",
                data_mode="live",
                pipeimob_connection="unavailable"
            )
            
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
    d_rec = tx.get("data_recebimento_comissao")
    if d_rec is not None and str(d_rec).strip() != "":
        return str(d_rec).strip(), "data_recebimento_comissao"
    
    d_pag = tx.get("data_pagamento_comissao")
    if d_pag is not None and str(d_pag).strip() != "":
        return str(d_pag).strip(), "data_pagamento_comissao"
        
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


def calculate_vgc_split(tx: dict) -> tuple[Decimal, Decimal, Decimal]:
    total_comm_raw = tx.get("total_comissao")
    if total_comm_raw is None:
        vgc_total = Decimal("0.0")
    else:
        try:
            vgc_total = Decimal(str(total_comm_raw))
        except Exception:
            vgc_total = Decimal("0.0")
            
    vgc_gralha = Decimal("0.0")
    comissionados = tx.get("comissionados")
    if isinstance(comissionados, list):
        for c in comissionados:
            if isinstance(c, dict):
                is_imob = c.get("comissionado_imobiliária")
                if is_imob is None:
                    is_imob = c.get("comissionado_imobiliaria")
                if is_imob is True or str(is_imob).lower() in ["true", "1"]:
                    val_raw = c.get("comissionado_valor") or c.get("valor") or 0.0
                    try:
                        vgc_gralha += Decimal(str(val_raw))
                    except Exception:
                        pass
                        
    vgc_demais_participantes = vgc_total - vgc_gralha
    return vgc_total, vgc_gralha, vgc_demais_participantes


def load_transactions_dataset(
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
    request_id: Optional[str] = None
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
        
    import uuid
    periodo = {
        "data_inicio_criacao": data_inicio_criacao,
        "data_fim_criacao": data_fim_criacao,
        "data_inicio_ccv": data_inicio_ccv,
        "data_fim_ccv": data_fim_ccv,
        "data_arquivamento_inicio": data_arquivamento_inicio,
        "data_arquivamento_fim": data_arquivamento_fim
    }
        
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
            "processing_time_ms": round(duration_ms, 2)
        }
        print(f"SECURE_LOG: {json.dumps(log_msg)}")
        return "demo", "synthetic_mock", dataset, 1
        
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
        
    # 1. Check transient cache for this live query filters
    cache_key = (
        "sales-cycle-v2-extremes",
        data_inicio_criacao, data_fim_criacao,
        data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim,
        codigo_imovel, codigo_contrato, transacao_unique_id
    )
    
    cached = dashboard_cache.get(cache_key)
    if cached is not None:
        live_txs, pages_fetched = cached
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
            "cache_hit": True,
            "processing_time_ms": round(duration_ms, 2)
        }
        print(f"SECURE_LOG: {json.dumps(log_msg)}")
        return "live", "pipeimob_api_v2", txs_to_return, pages_fetched

    api_key = os.getenv("PIPEIMOB_API_KEY").strip()
    api_secret = os.getenv("PIPEIMOB_SECRET_KEY").strip()
        
    live_txs, pages_fetched = fetch_all_pipeimob_transactions(
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
    
    if not live_txs:
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Pipeimob CRM API returned empty transactions dataset.",
            error_code="invalid_pipeimob_response",
            data_mode="live",
            pipeimob_connection="unavailable"
        )
        
    # Enrich comissao_imobiliaria and map data_recebimento_comissao defensively for live transactions
    for tx in live_txs:
        _, vgc_gralha, _ = calculate_vgc_split(tx)
        tx["comissao_imobiliaria"] = float(vgc_gralha)
        if "data_recebimento_comissao" not in tx or tx.get("data_recebimento_comissao") is None:
            tx["data_recebimento_comissao"] = tx.get("data_pagamento_comissao")
        
    # Set cache
    dashboard_cache.set(cache_key, (live_txs, pages_fetched))
    
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
        "cache_hit": False,
        "processing_time_ms": round(duration_ms, 2)
    }
    print(f"SECURE_LOG: {json.dumps(log_msg)}")
    return "live", "pipeimob_api_v2", txs_to_return, pages_fetched
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

def compute_dashboard_aggregates(
    filtered: list,
    data_inicio_ccv: Optional[str] = None,
    data_fim_ccv: Optional[str] = None,
    data_inicio_criacao: Optional[str] = None,
    data_fim_criacao: Optional[str] = None
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
    
    avg_rate = float(round((total_commissions / total_sales) * 100, 2)) if total_sales > 0 else 0.0
    
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
    ratio = float(round((financed_count / total_deals) * 100, 2)) if total_deals > 0 else 0.0
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
        rate = float(round((comm / val) * 100, 2)) if val > 0 else 0.0
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
    avg_rate_comm = float(round((total_comm / total_sales_comm) * 100, 2)) if total_sales_comm > 0 else 0.0
    commissions_payload = {
        "total_commissions": float(round(total_comm, 2)),
        "avg_commission_rate": avg_rate_comm,
        "commissions": commissions
    }
    
    # 7. Timeline
    start_str = data_inicio_ccv or data_inicio_criacao
    end_str = data_fim_ccv or data_fim_criacao
    
    start_ym = parse_date_to_year_month(start_str) if start_str else None
    end_ym = parse_date_to_year_month(end_str) if end_str else None
    
    if not start_ym or not end_ym:
        dataset_yms = []
        for tx in filtered:
            dt_str = extract_transaction_date(tx)
            ym = parse_date_to_year_month(dt_str)
            if ym:
                dataset_yms.append(ym)
        if dataset_yms:
            if not start_ym:
                start_ym = min(dataset_yms)
            if not end_ym:
                end_ym = max(dataset_yms)
        else:
            now = datetime.now()
            if not start_ym:
                start_ym = (now.year, now.month)
            if not end_ym:
                end_ym = (now.year, now.month)
                
    start_year, start_month = start_ym
    end_year, end_month = end_ym
    
    if (start_year, start_month) > (end_year, end_month):
        start_year, start_month, end_year, end_month = end_year, end_month, start_year, start_month
        
    months_range = []
    curr_year, curr_month = start_year, start_month
    while (curr_year, curr_month) <= (end_year, end_month):
        months_range.append((curr_year, curr_month))
        if curr_month == 12:
            curr_month = 1
            curr_year += 1
        else:
            curr_month += 1
            
    timeline_groups = {}
    for y, m in months_range:
        key = f"{y}-{m:02d}"
        timeline_groups[key] = {
            "count": 0,
            "sales": Decimal("0.0"),
            "commissions": Decimal("0.0")
        }
        
    registros_com_data = 0
    registros_sem_data = 0
    datas_invalidas = 0
    found_fields = set()
    
    unclassified_groups = {
        "count": 0,
        "sales": Decimal("0.0"),
        "commissions": Decimal("0.0"),
        "missing_date_count": 0,
        "invalid_date_count": 0,
        "out_of_range_count": 0
    }
    
    for tx in filtered:
        dt_str = extract_transaction_date(tx)
        ym = None
        is_missing = False
        is_invalid = False
        
        if dt_str:
            ym = parse_date_to_year_month(dt_str)
            if not ym:
                datas_invalidas += 1
                is_invalid = True
        else:
            registros_sem_data += 1
            is_missing = True
            
        priority_keys = [
            "data_assinatura_ccv",
            "data_ccv",
            "data_assinatura",
            "data_contrato",
            "data_criacao",
            "created_at"
        ]
        found_key = "other"
        for pk in priority_keys:
            if tx.get(pk) is not None:
                found_key = pk
                break
        found_fields.add(found_key)
        
        val_sales = Decimal(str(tx.get("valor_contrato") or "0.0"))
        val_comm = Decimal(str(tx.get("total_comissao") or "0.0"))
        
        if ym:
            registros_com_data += 1
            y, m = ym
            key = f"{y}-{m:02d}"
            
            if key in timeline_groups:
                timeline_groups[key]["count"] += 1
                timeline_groups[key]["sales"] += val_sales
                timeline_groups[key]["commissions"] += val_comm
            else:
                unclassified_groups["count"] += 1
                unclassified_groups["sales"] += val_sales
                unclassified_groups["commissions"] += val_comm
                unclassified_groups["out_of_range_count"] += 1
        else:
            unclassified_groups["count"] += 1
            unclassified_groups["sales"] += val_sales
            unclassified_groups["commissions"] += val_comm
            if is_missing:
                unclassified_groups["missing_date_count"] += 1
            if is_invalid:
                unclassified_groups["invalid_date_count"] += 1

    log_data = {
        "event": "timeline_computed",
        "registros_com_data": registros_com_data,
        "registros_sem_data": registros_sem_data,
        "datas_invalidas": datas_invalidas,
        "campos_data_encontrados": list(found_fields),
        "quantidade_por_mes": {k: v["count"] for k, v in timeline_groups.items()},
        "quantidade_nao_classificada": unclassified_groups["count"]
    }
    print(f"SECURE_LOG: {json.dumps(log_data)}")
    
    timeline = []
    for key in sorted(timeline_groups.keys()):
        parts = key.split("-")
        y = parts[0]
        m = parts[1]
        label = f"{months_pt.get(m, m)}/{y[-2:]}"
        stats = timeline_groups[key]
        timeline.append({
            "month": key,
            "label": label,
            "transaction_count": stats["count"],
            "total_sales": f"{stats['sales']:.2f}",
            "total_commissions": f"{stats['commissions']:.2f}"
        })
        
    timeline_count_sum = sum(t["transaction_count"] for t in timeline)
    timeline_sales_sum = sum(Decimal(t["total_sales"]) for t in timeline)
    timeline_comm_sum = sum(Decimal(t["total_commissions"]) for t in timeline)
    
    unclassified_count = unclassified_groups["count"]
    unclassified_sales = unclassified_groups["sales"]
    unclassified_comm = unclassified_groups["commissions"]
    
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
        "missing_date_count": unclassified_groups["missing_date_count"],
        "invalid_date_count": unclassified_groups["invalid_date_count"],
        "out_of_range_count": unclassified_groups["out_of_range_count"]
    }
    
    # === VGC Commission Financials Analysis ===
    sp_tz = ZoneInfo("America/Sao_Paulo")
    as_of_datetime = datetime.now(sp_tz)
    as_of_date_obj = as_of_datetime.date()
    as_of_date_str = as_of_date_obj.strftime("%Y-%m-%d")
    
    # Initialize totals
    tot_vgc_total = Decimal("0.0")
    tot_gralha = Decimal("0.0")
    tot_demais = Decimal("0.0")
    
    # Source counters
    receipt_date_sources = {
        "data_recebimento_comissao": 0,
        "data_pagamento_comissao": 0,
        "missing": 0
    }
    
    # Classification counters & sums
    received_total = Decimal("0.0")
    received_gralha = Decimal("0.0")
    received_demais = Decimal("0.0")
    received_count = 0
    
    pending_total = Decimal("0.0")
    pending_gralha = Decimal("0.0")
    pending_demais = Decimal("0.0")
    pending_count = 0
    pending_future_date_count = 0
    pending_without_date_count = 0
    
    unknown_total = Decimal("0.0")
    unknown_gralha = Decimal("0.0")
    unknown_demais = Decimal("0.0")
    unknown_count = 0
    unknown_invalid_date_count = 0
    
    for tx in filtered:
        vgc_total, vgc_gralha, vgc_demais = calculate_vgc_split(tx)
        
        tot_vgc_total += vgc_total
        tot_gralha += vgc_gralha
        tot_demais += vgc_demais
        
        # Get receipt date and source
        date_str, source = get_receipt_date(tx)
        
        if source == "data_recebimento_comissao":
            receipt_date_sources["data_recebimento_comissao"] += 1
        elif source == "data_pagamento_comissao":
            receipt_date_sources["data_pagamento_comissao"] += 1
        else:
            receipt_date_sources["missing"] += 1
            
        if date_str is None:
            # C. A RECEBER - SEM DATA
            pending_total += vgc_total
            pending_gralha += vgc_gralha
            pending_demais += vgc_demais
            pending_count += 1
            pending_without_date_count += 1
        else:
            # Parse the date using parse_explicit_date
            receipt_date = parse_explicit_date(date_str)
            if receipt_date is not None:
                if receipt_date <= as_of_date_obj:
                    # A. RECEBIDA
                    received_total += vgc_total
                    received_gralha += vgc_gralha
                    received_demais += vgc_demais
                    received_count += 1
                else:
                    # B. A RECEBER - DATA FUTURA
                    pending_total += vgc_total
                    pending_gralha += vgc_gralha
                    pending_demais += vgc_demais
                    pending_count += 1
                    pending_future_date_count += 1
            else:
                # D. SITUAÇÃO DESCONHECIDA
                unknown_total += vgc_total
                unknown_gralha += vgc_gralha
                unknown_demais += vgc_demais
                unknown_count += 1
                unknown_invalid_date_count += 1
                
    # Reconciliations
    diff1 = abs((tot_gralha + tot_demais) - tot_vgc_total)
    diff2 = abs((received_total + pending_total + unknown_total) - tot_vgc_total)
    diff3 = abs((received_gralha + pending_gralha + unknown_gralha) - tot_gralha)
    diff4 = abs((received_demais + pending_demais + unknown_demais) - tot_demais)
    
    reconciliation_difference = diff1 + diff2 + diff3 + diff4
    reconciled = (reconciliation_difference == Decimal("0.0"))
    
    # Audit quantity reconciliations
    quantity_reconciled = (
        (received_count + pending_count + unknown_count == len(filtered)) and
        (pending_future_date_count + pending_without_date_count == pending_count)
    )
    if not quantity_reconciled or tot_gralha > tot_vgc_total or tot_gralha < 0 or tot_demais < 0:
        reconciled = False
        
    received_ratio = float(received_total / tot_vgc_total) if tot_vgc_total > 0 else 0.0
    
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
            "reconciliation_difference": f"{reconciliation_difference:.2f}",
            "reconciled": reconciled
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
            "future_date_count": pending_future_date_count,
            "without_date_count": pending_without_date_count
        },
        "unknown": {
            "total": f"{unknown_total:.2f}",
            "gralha": f"{unknown_gralha:.2f}",
            "demais_participantes": f"{unknown_demais:.2f}",
            "transaction_count": unknown_count,
            "invalid_date_count": unknown_invalid_date_count
        },
        "received_ratio": received_ratio,
        "semantic_validation": "provisional_v1",
        "disclaimer": (
            "Versão 1: a classificação considera como recebido o VGC com data "
            "registrada até a data de referência. Datas futuras ou ausentes são "
            "consideradas a receber. A fonte financeira definitiva aguarda confirmação "
            "do Pipeimob."
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
        "received_count": received_count,
        "pending_future_date_count": pending_future_date_count,
        "pending_without_date_count": pending_without_date_count,
        "unknown_invalid_date_count": unknown_invalid_date_count
    }
    print(f"SECURE_LOG: {json.dumps(vgc_log)}")

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
        "sales_cycle": sales_cycle
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
        "endereco_bairro": tx.get("endereco_bairro"),
        "endereco_cidade": tx.get("endereco_cidade"),
        "endereco_uf": tx.get("endereco_uf"),
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
    future_date_count: int
    without_date_count: int

class VGCUnknown(BaseModel):
    total: str
    gralha: str
    demais_participantes: str
    transaction_count: int
    invalid_date_count: int

class ReceiptDateSources(BaseModel):
    data_recebimento_comissao: int
    data_pagamento_comissao: int
    missing: int

class CommissionFinancials(BaseModel):
    period_basis: str = "ccv"
    as_of_date: str
    timezone: str = "America/Sao_Paulo"
    calculation_method: str = "registered_receipt_date_v1"
    allocation_method: str = "status_only"
    receipt_date_sources: ReceiptDateSources
    vgc_total: str
    composition: VGCComposition
    received: VGCReceived
    pending: VGCPending
    unknown: VGCUnknown
    received_ratio: float
    semantic_validation: str = "provisional_v1"
    disclaimer: Optional[str] = None

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

class DashboardFullResponse(BaseModel):
    data_mode: str
    source: str
    period: DashboardPeriod
    pages_fetched: int
    transaction_count: int
    summary: SummaryDataPayload
    origins: List[OriginMetric]
    stages: List[StageMetric]
    managers: List[ManagerMetric]
    payments: PaymentsDataPayload
    commissions: CommissionsDataPayload
    timeline: List[TimelineMetric]
    unclassified: Optional[UnclassifiedTimeline] = None
    reconciliation: Optional[TimelineReconciliation] = None
    sales_cycle: Optional[SalesCyclePayload] = None
    schema_version: Optional[str] = "1.0"
    generated_at: Optional[str] = None
    filters_applied: Optional[dict] = None
    commission_financials: Optional[CommissionFinancials] = None
    debug_metrics: Optional[dict] = None

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

_jwk_client = None

def get_jwk_client():
    global _jwk_client
    if _jwk_client is None:
        jwks_url = os.getenv("SUPABASE_JWKS_URL")
        if jwks_url:
            from jwt import PyJWKClient
            _jwk_client = PyJWKClient(jwks_url)
    return _jwk_client

async def verify_backend_api_key(
    authorization: Optional[str] = Header(None),
    x_backend_api_key: Optional[str] = Header(None)
):
    # Server-to-server fallback bypass
    expected_server_key = os.getenv("BACKEND_API_KEY")
    if expected_server_key and x_backend_api_key == expected_server_key:
        return {"email": "server-to-server@gralhaimoveis.com.br", "sub": "server-to-server"}

    import jwt

    if not authorization:
        raise AuthException(
            status_code=401,
            detail="Authentication required.",
            error_code="authentication_required"
        )
        
    if not authorization.startswith("Bearer "):
        raise AuthException(
            status_code=401,
            detail="Invalid or expired access token.",
            error_code="invalid_access_token"
        )
        
    token = authorization.split(" ")[1]
    
    try:
        app_env = os.getenv("APP_ENV", "production").lower()
        jwks_url = os.getenv("SUPABASE_JWKS_URL")
        
        if not jwks_url and app_env == "production":
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )
            
        if jwks_url:
            # 1. Pre-verify token structure and kid header if JWKS is used
            try:
                header = jwt.get_unverified_header(token)
                if not isinstance(header, dict) or "kid" not in header:
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
                
            from jwt.exceptions import PyJWKClientConnectionError
            client = get_jwk_client()
            try:
                jwk_set = client.get_jwk_set()
            except PyJWKClientConnectionError:
                raise AuthException(
                    status_code=503,
                    detail="Supabase project does not expose asymmetric JWT signing keys.",
                    error_code="supabase_jwks_unavailable"
                )
            except Exception:
                raise AuthException(
                    status_code=503,
                    detail="Supabase project does not expose asymmetric JWT signing keys.",
                    error_code="supabase_jwks_unavailable"
                )
                
            if not jwk_set.keys:
                raise AuthException(
                    status_code=503,
                    detail="Supabase project does not expose asymmetric JWT signing keys.",
                    error_code="supabase_jwks_unavailable"
                )
                
            try:
                signing_key = client.get_signing_key_from_jwt(token)
            except Exception:
                raise AuthException(
                    status_code=401,
                    detail="Invalid or expired access token.",
                    error_code="invalid_access_token"
                )
            
            aud = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
            iss = os.getenv("SUPABASE_ISSUER")
            
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience=aud,
                issuer=iss,
                options={"require": ["exp", "iss", "aud", "sub"]}
            )
        else:
            # Dev/Test fallback: decode without signature verification
            # but manually validate claims to align with production checks
            payload = jwt.decode(
                token,
                options={"verify_signature": False}
            )
            
            # Explicit exp check
            if "exp" in payload:
                import time
                if payload["exp"] < time.time():
                    raise jwt.ExpiredSignatureError("Token has expired")
            else:
                raise jwt.MissingRequiredClaimError("exp")
                
            # Explicit iss check
            expected_iss = os.getenv("SUPABASE_ISSUER")
            if expected_iss and payload.get("iss") != expected_iss:
                raise jwt.InvalidIssuerError("Invalid issuer")
                
            # Explicit aud check
            expected_aud = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
            if payload.get("aud") != expected_aud:
                raise jwt.InvalidAudienceError("Invalid audience")

            # Explicit sub check
            if not payload.get("sub"):
                raise jwt.MissingRequiredClaimError("sub")

        # Additional required claims validation (both JWKS & Dev)
        # 1. role must be authenticated
        if payload.get("role") != "authenticated":
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )
            
        # 2. email must be present
        if not payload.get("email") or not isinstance(payload.get("email"), str):
            raise AuthException(
                status_code=401,
                detail="Invalid or expired access token.",
                error_code="invalid_access_token"
            )
            
    except AuthException:
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(transacao_unique_id=id, request_id=req_id)
    validate_dataset_origin(mode, src, dataset)
    
    target_tx = None
    for tx in dataset:
        if tx.get("transacao_unique_id_pipeimob") == id or tx.get("codigo_contrato") == id:
            target_tx = tx
            break
            
    if not target_tx:
        try:
            mode, src, dataset, pages_fetched = load_transactions_dataset(codigo_contrato=id, request_id=req_id)
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    etapa_atual: Optional[str] = Query(None)
):
    req_id = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
    mode, src, dataset, pages_fetched = load_transactions_dataset(
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
    
    enable_debug = os.getenv("ENABLE_SAFE_DEBUG_METRICS", "false").strip().lower() == "true"
    debug_metrics = None
    if enable_debug and dataset:
        debug_metrics = {}
        debug_metrics["transaction_count"] = len(dataset)
        
        # 1. Top-level keys presence counts
        top_keys_counts = {}
        for tx in dataset:
            for k in tx.keys():
                top_keys_counts[k] = top_keys_counts.get(k, 0) + 1
        debug_metrics["top_level_keys_counts"] = top_keys_counts
        
        # 2. Priority keys presence, types and validity counts
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
        
        # Recurse checking
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
        
        # 3. Stage counts validation
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
        "etapa_atual": etapa_atual
    }
    filters_applied = {k: v for k, v in filters_map.items() if v is not None}

    return DashboardFullResponse(
        data_mode=mode,
        source=src,
        period=DashboardPeriod(start=data_inicio_ccv, end=data_fim_ccv),
        pages_fetched=pages_fetched,
        transaction_count=len(filtered),
        summary=SummaryDataPayload(**aggregates["summary"]),
        origins=[OriginMetric(**o) for o in aggregates["origins"]],
        stages=[StageMetric(**s) for s in aggregates["stages"]],
        managers=[ManagerMetric(**m) for m in aggregates["managers"]],
        payments=PaymentsDataPayload(**aggregates["payments"]),
        commissions=CommissionsDataPayload(**aggregates["commissions"]),
        timeline=[TimelineMetric(**t) for t in aggregates["timeline"]],
        unclassified=UnclassifiedTimeline(**aggregates["unclassified"]),
        reconciliation=TimelineReconciliation(**aggregates["reconciliation"]),
        sales_cycle=SalesCyclePayload(**aggregates["sales_cycle"]) if aggregates.get("sales_cycle") is not None else None,
        schema_version="1.0",
        generated_at=generated_at_utc,
        filters_applied=filters_applied,
        commission_financials=CommissionFinancials(**aggregates["commission_financials"]),
        debug_metrics=debug_metrics
    )
