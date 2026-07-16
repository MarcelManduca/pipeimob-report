import os
import urllib.request
import json
import ssl
import time
from datetime import datetime, timezone
from typing import List, Optional, Union
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

# Import our synthetic anonymous mock database
from mock_data import MOCK_TRANSACTIONS

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

# Live transaction page fetcher (concurrent thread execution)
def fetch_all_transactions_live(
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
    transacao_unique_id: Optional[str] = None,
    pagina: Optional[int] = None
) -> list:
    token = get_auth_token(api_key, api_secret)
    if not token:
        # Fallback in case auth somehow passed without returning a token
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
    
    # We define a request helper that supports 401 retry exactly once
    def request_with_retry(url: str, retry_allowed: bool = True) -> dict:
        nonlocal token
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {token}', 'User-Agent': 'Mozilla/5.0'}
        )
        try:
            with urllib.request.urlopen(req, context=ssl_context, timeout=8) as response:
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
                # Force reauthentication once
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

    start_page = pagina if pagina is not None else 1
    url_p1 = f"{BASE_URL}/negocios/transacoes?pagina={start_page}{prefix}"
    res_body = request_with_retry(url_p1)
    
    # Read transactions safely from response.data.transacoes
    txs = res_body.get("data", {}).get("transacoes", []) if isinstance(res_body.get("data"), dict) else []
    all_transactions = list(txs)
    
    # Read pagination metadata defensively:
    # 1. response.meta.pagination
    # 2. response.data.meta.pagination
    meta_p = None
    if "meta" in res_body and isinstance(res_body["meta"], dict) and "pagination" in res_body["meta"]:
        meta_p = res_body["meta"]["pagination"]
    elif "data" in res_body and isinstance(res_body["data"], dict) and "meta" in res_body["data"] and isinstance(res_body["data"]["meta"], dict) and "pagination" in res_body["data"]["meta"]:
        meta_p = res_body["data"]["meta"]["pagination"]
        
    if meta_p is None:
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Pagination metadata (meta.pagination or data.meta.pagination) not found in Pipeimob response.",
            error_code="invalid_pipeimob_response",
            data_mode="live",
            pipeimob_connection="unavailable"
        )
        
    total_pages = meta_p.get("total_pages") or 1

    def fetch_page_worker(p):
        url = f"{BASE_URL}/negocios/transacoes?pagina={p}{prefix}"
        try:
            body = request_with_retry(url, retry_allowed=True)
            return body.get("data", {}).get("transacoes", []) if isinstance(body.get("data"), dict) else []
        except Exception:
            pass
        return []

    max_pages = 12
    pages_to_fetch = range(start_page + 1, min(total_pages + 1, start_page + max_pages))
    
    if pages_to_fetch:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(pages_to_fetch)) as executor:
            futures = [executor.submit(fetch_page_worker, p) for p in pages_to_fetch]
            for fut in futures:
                all_transactions.extend(fut.result())
                
    return all_transactions

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

# Master dataset loader helper (with strict mode verification)
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
    pagina: Optional[int] = None
) -> tuple:
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
        return "demo", "synthetic_mock", MOCK_TRANSACTIONS
        
    # Live mode: validate that at least one direct filter is present.
    # The 'pagina' parameter is a pagination parameter and does NOT satisfy this requirement on its own.
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
        
    api_key = os.getenv("PIPEIMOB_API_KEY").strip()
    api_secret = os.getenv("PIPEIMOB_SECRET_KEY").strip()
        
    live_txs = fetch_all_transactions_live(
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
        transacao_unique_id=transacao_unique_id,
        pagina=pagina
    )
    
    if not live_txs:
        raise IntegrationUnavailableError(
            status_code=503,
            detail="Pipeimob CRM API returned empty transactions dataset.",
            error_code="invalid_pipeimob_response",
            data_mode="live",
            pipeimob_connection="unavailable"
        )
        
    # Enrich comissao_imobiliaria defensively for live transactions
    for tx in live_txs:
        comm_imob = 0.0
        comissionados = tx.get("comissionados") or []
        for c in comissionados:
            is_imob = c.get("comissionado_imobiliária")
            if is_imob is None:
                is_imob = c.get("comissionado_imobiliaria")
            if is_imob is True or str(is_imob).lower() in ["true", "1"]:
                val = c.get("comissionado_valor") or 0.0
                comm_imob += float(val)
        tx["comissao_imobiliaria"] = comm_imob
        
    return "live", "pipeimob_api_v2", live_txs

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
                forma_pagamento_clean.append({"nome": nome, "valor": float(valor)})
                
    # 4. comissionados sanitizados (apenas nome, tipo e valor)
    comissionados_raw = tx.get("comissionados") or []
    comissionados_clean = []
    if isinstance(comissionados_raw, list):
        for c in comissionados_raw:
            if isinstance(c, dict):
                nome = c.get("nome") or c.get("comissionado_nome") or "Comissionado"
                tipo = c.get("tipo") or c.get("participacao") or c.get("papel") or "Corretor"
                valor = c.get("valor") or c.get("comissionado_valor") or 0.0
                comissionados_clean.append({
                    "nome": nome,
                    "tipo": tipo,
                    "valor": float(valor)
                })

    # 5. Build sanitized object
    return {
        "transacao_unique_id_pipeimob": tx.get("transacao_unique_id_pipeimob"),
        "codigo_contrato": tx.get("codigo_contrato"),
        "codigo_imovel": tx.get("codigo_imovel"),
        "data_contrato": tx.get("data_contrato"),
        "data_inicio_venda": tx.get("data_inicio_venda"),
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
    month: str = Field(..., description="Month/Year label", json_schema_extra={"example": "Jan/26"})
    volume: float = Field(..., description="Sales volume during the month", json_schema_extra={"example": 15000000.0})
    count: int = Field(..., description="Number of transactions during the month", json_schema_extra={"example": 12})

class TimelineDataPayload(BaseModel):
    timeline: List[TimelineMetric] = Field(...)

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
        status="validated_live",
        implemented=True,
        validated=True,
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
            try:
                client = get_jwk_client()
                signing_key = client.get_signing_key_from_jwt(token)
            except Exception:
                raise AuthException(
                    status_code=503,
                    detail="Supabase project does not expose asymmetric JWT signing keys.",
                    error_code="supabase_jwks_unavailable"
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
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
    response: Response
):
    # In live mode we must pass at least one direct filter, so we pass both or try to load by transacao_unique_id or codigo_contrato
    mode, src, dataset = load_transactions_dataset(transacao_unique_id=id)
    
    target_tx = None
    for tx in dataset:
        if tx.get("transacao_unique_id_pipeimob") == id or tx.get("codigo_contrato") == id:
            target_tx = tx
            break
            
    if not target_tx:
        try:
            mode, src, dataset = load_transactions_dataset(codigo_contrato=id)
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )
    
    total_sales = sum(float(tx.get("valor_contrato") or 0.0) for tx in filtered)
    total_commissions = sum(float(tx.get("total_comissao") or 0.0) for tx in filtered)
    avg_rate = float(round((total_commissions / total_sales) * 100, 2)) if total_sales > 0 else 0.0
    
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = SummaryDataPayload(
        total_sales=float(round(total_sales, 2)),
        total_commissions=float(round(total_commissions, 2)),
        avg_commission_rate=avg_rate,
        transaction_count=len(filtered)
    )
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )
    
    groups = {}
    for tx in filtered:
        origin = tx.get("midia_origem_compradores") or "Não Informado"
        val = float(tx.get("valor_contrato") or 0.0)
        if origin not in groups:
            groups[origin] = {"volume": 0.0, "count": 0}
        groups[origin]["volume"] += val
        groups[origin]["count"] += 1
        
    origins_list = [
        OriginMetric(origin=o, count=stats["count"], volume=float(round(stats["volume"], 2)))
        for o, stats in groups.items()
    ]
    origins_list.sort(key=lambda x: x.volume, reverse=True)
    
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = OriginsDataPayload(origins=origins_list)
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )
    
    groups = {}
    for tx in filtered:
        stage = tx.get("etapa_atual") or "Sem Etapa"
        val = float(tx.get("valor_contrato") or 0.0)
        if stage not in groups:
            groups[stage] = {"volume": 0.0, "count": 0}
        groups[stage]["volume"] += val
        groups[stage]["count"] += 1
        
    stages_list = [
        StageMetric(stage=s, count=stats["count"], volume=float(round(stats["volume"], 2)))
        for s, stats in groups.items()
    ]
    stages_list.sort(key=lambda x: x.volume, reverse=True)
    
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = StagesDataPayload(stages=stages_list)
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )
    
    groups = {}
    for tx in filtered:
        mgr = tx.get("agente_gestor") or "Sem Gestor"
        val = float(tx.get("valor_contrato") or 0.0)
        if mgr not in groups:
            groups[mgr] = {"volume": 0.0, "count": 0}
        groups[mgr]["volume"] += val
        groups[mgr]["count"] += 1
        
    managers_list = []
    for mgr, stats in groups.items():
        ticket = float(round(stats["volume"] / stats["count"], 2)) if stats["count"] > 0 else 0.0
        managers_list.append(
            ManagerMetric(
                manager=mgr,
                count=stats["count"],
                volume=float(round(stats["volume"], 2)),
                ticket_medio=ticket
            )
        )
    managers_list.sort(key=lambda x: x.volume, reverse=True)
    
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = ManagersDataPayload(managers=managers_list)
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )
    
    financed_count = 0
    cash_count = 0
    bank_groups = {}
    method_groups = {}
    
    for tx in filtered:
        is_fin = tx.get("financiamento", False)
        val = float(tx.get("valor_contrato") or 0.0)
        
        if is_fin:
            financed_count += 1
            bank = tx.get("financiamento_banco") or "Não Informado"
            if bank not in bank_groups:
                bank_groups[bank] = {"volume": 0.0, "count": 0}
            bank_groups[bank]["volume"] += val
            bank_groups[bank]["count"] += 1
        else:
            cash_count += 1
            
        for fp in tx.get("forma_pagamento", []):
            m_name = fp.get("nome") or "Outros"
            m_val = float(fp.get("valor") or 0.0)
            method_groups[m_name] = method_groups.get(m_name, 0.0) + m_val
            
    total_deals = financed_count + cash_count
    ratio = float(round((financed_count / total_deals) * 100, 2)) if total_deals > 0 else 0.0
    
    banks_list = [
        BankMetric(bank=b, count=stats["count"], volume=float(round(stats["volume"], 2)))
        for b, stats in bank_groups.items()
    ]
    banks_list.sort(key=lambda x: x.volume, reverse=True)
    
    methods_list = [
        PaymentMethodMetric(method=m, volume=float(round(v, 2)))
        for m, v in method_groups.items()
    ]
    methods_list.sort(key=lambda x: x.volume, reverse=True)
    
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = PaymentsDataPayload(
        financed_count=financed_count,
        cash_count=cash_count,
        financing_ratio=ratio,
        banks=banks_list,
        methods=methods_list
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )
    
    total_comm = 0.0
    total_sales = 0.0
    commissions_list = []
    
    for tx in filtered:
        val = float(tx.get("valor_contrato") or 0.0)
        comm = float(tx.get("total_comissao") or 0.0)
        rate = float(round((comm / val) * 100, 2)) if val > 0 else 0.0
        
        total_comm += comm
        total_sales += val
        
        commissions_list.append(
            CommissionMetric(
                transaction_id=tx.get("transacao_unique_id_pipeimob") or "",
                contract_code=tx.get("codigo_contrato") or "",
                value=val,
                commission=comm,
                rate=rate,
                manager=tx.get("agente_gestor") or "Sem Gestor"
            )
        )
        
    avg_rate = float(round((total_comm / total_sales) * 100, 2)) if total_sales > 0 else 0.0
    
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = CommissionsDataPayload(
        total_commissions=float(round(total_comm, 2)),
        avg_commission_rate=avg_rate,
        commissions=commissions_list
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
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        pagina
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv,
        data_arquivamento_inicio, data_arquivamento_fim, codigo_imovel, codigo_contrato, transacao_unique_id,
        agent, category, financing, etapa_atual
    )
    
    timeline_groups = {}
    for tx in filtered:
        tx_date_str = tx.get("data_inicio_venda") or tx.get("data_contrato") or ""
        if not tx_date_str:
            continue
        try:
            parts = tx_date_str.split("-")
            if len(parts) >= 2:
                key = f"{parts[0]}-{parts[1]}"
                val = float(tx.get("valor_contrato") or 0.0)
                if key not in timeline_groups:
                    timeline_groups[key] = {"volume": 0.0, "count": 0}
                timeline_groups[key]["volume"] += val
                timeline_groups[key]["count"] += 1
        except Exception:
            pass
            
    sorted_keys = sorted(timeline_groups.keys())
    timeline_list = []
    for key in sorted_keys:
        parts = key.split("-")
        year = parts[0][-2:]
        month = parts[1]
        label = f"{MONTHS_PT.get(month, month)}/{year}"
        timeline_list.append(
            TimelineMetric(
                month=label,
                volume=float(round(timeline_groups[key]["volume"], 2)),
                count=timeline_groups[key]["count"]
            )
        )
        
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = TimelineDataPayload(timeline=timeline_list)
    return meta
