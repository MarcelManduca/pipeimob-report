import os
import urllib.request
import json
import ssl
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import FastAPI, Header, Query, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Import our synthetic anonymous mock database
from mock_data import MOCK_TRANSACTIONS

# Load environment variables
load_dotenv()

app = FastAPI(
    title="Pipeimob Report API",
    description="Backend API for Pipeimob reports and Lovable integration (BI Dashboard - Phase 2)",
    version="0.1.0",
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
def get_auth_token(api_key: str, api_secret: str) -> Optional[str]:
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
        with urllib.request.urlopen(req, context=ssl_context) as response:
            res_body = json.loads(response.read().decode('utf-8'))
            if res_body.get("success"):
                return res_body.get("data", {}).get("access_token")
    except Exception as e:
        print(f"Error authenticating with Pipeimob: {e}")
    return None

# Live transaction page fetcher (concurrent thread execution)
def fetch_all_transactions_live(
    api_key: str, 
    api_secret: str, 
    data_inicio_criacao: Optional[str] = None,
    data_fim_criacao: Optional[str] = None,
    data_inicio_ccv: Optional[str] = None,
    data_fim_ccv: Optional[str] = None,
    data_arquivamento_inicio: Optional[str] = None,
    data_arquivamento_fim: Optional[str] = None
) -> list:
    token = get_auth_token(api_key, api_secret)
    if not token:
        return []
        
    query_parts = []
    if data_inicio_criacao: query_parts.append(f"data_inicio_criacao={data_inicio_criacao}")
    if data_fim_criacao: query_parts.append(f"data_fim_criacao={data_fim_criacao}")
    if data_inicio_ccv: query_parts.append(f"data_inicio_ccv={data_inicio_ccv}")
    if data_fim_ccv: query_parts.append(f"data_fim_ccv={data_fim_ccv}")
    if data_arquivamento_inicio: query_parts.append(f"data_arquivamento_inicio={data_arquivamento_inicio}")
    if data_arquivamento_fim: query_parts.append(f"data_arquivamento_fim={data_arquivamento_fim}")
    
    query_str = "&".join(query_parts)
    prefix = f"&{query_str}" if query_str else ""
    
    url_p1 = f"{BASE_URL}/negocios/transacoes?pagina=1{prefix}"
    req_p1 = urllib.request.Request(
        url_p1,
        headers={'Authorization': f'Bearer {token}', 'User-Agent': 'Mozilla/5.0'}
    )
    
    all_transactions = []
    total_pages = 1
    try:
        with urllib.request.urlopen(req_p1, context=ssl_context) as response:
            res_body = json.loads(response.read().decode('utf-8'))
            if res_body.get("success"):
                txs = res_body.get("data", {}).get("transacoes", [])
                all_transactions.extend(txs)
                pagination = res_body.get("meta", {}).get("pagination", {})
                total_pages = pagination.get("total_pages", 1)
    except Exception as e:
        print(f"Error fetching page 1 of transactions: {e}")
        return []

    # If first page failed, treat integration as failed
    if not all_transactions:
        return []

    def fetch_page_worker(p):
        url = f"{BASE_URL}/negocios/transacoes?pagina={p}{prefix}"
        req = urllib.request.Request(
            url,
            headers={'Authorization': f'Bearer {token}', 'User-Agent': 'Mozilla/5.0'}
        )
        try:
            with urllib.request.urlopen(req, context=ssl_context) as response:
                res_body = json.loads(response.read().decode('utf-8'))
                if res_body.get("success"):
                    return res_body.get("data", {}).get("transacoes", [])
        except Exception as e:
            print(f"Error page {p}: {e}")
        return []

    max_pages = 12
    pages_to_fetch = range(2, min(total_pages + 1, max_pages + 1))
    
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
            return "live", "pending"
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
    data_arquivamento_fim: Optional[str] = None
) -> tuple:
    data_mode, conn_status = get_current_data_mode_and_connection()
    
    if data_mode == "unconfigured":
        raise HTTPException(
            status_code=503,
            detail="Configuration pending. Please set PIPEIMOB_DATA_MODE environment variable."
        )
        
    if data_mode == "demo":
        return "demo", "synthetic_mock", MOCK_TRANSACTIONS
        
    # Live mode: require credentials. Never fallback silently to mock.
    if conn_status == "missing_credentials":
        raise HTTPException(
            status_code=400, 
            detail="Configuration pending. Valid Pipeimob API credentials are required in Live mode."
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
        data_arquivamento_fim=data_arquivamento_fim
    )
    
    if not live_txs:
        raise HTTPException(
            status_code=503, 
            detail="Pipeimob connection failed or returned no data. Live request aborted."
        )
        
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
    agent: Optional[str] = None,
    category: Optional[str] = None,
    financing: Optional[bool] = None,
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
                
        filtered.append(tx)
    return filtered

# Pydantic Schemas for OpenAPI documentation
class HealthResponse(BaseModel):
    status: str = Field(..., description="Status of the API service", json_schema_extra={"example": "ok"})
    service: str = Field(..., description="Name of the service", json_schema_extra={"example": "pipeimob-report"})
    version: str = Field(..., description="Version of the service", json_schema_extra={"example": "0.1.0"})
    api_version: str = Field(..., description="API version of the service", json_schema_extra={"example": "v2"})
    pipeimob_connection: str = Field(..., description="Connection status to Pipeimob CRM", json_schema_extra={"example": "not_tested"})
    data_mode: str = Field(..., description="Active data mode: demo or live", json_schema_extra={"example": "demo"})
    timestamp: str = Field(..., description="Current timestamp in UTC ISO-8601 format", json_schema_extra={"example": "2026-01-01T00:00:00Z"})

class ResourceCatalog(BaseModel):
    id: str = Field(..., description="Unique resource ID", json_schema_extra={"example": "transactions"})
    name: str = Field(..., description="Resource name", json_schema_extra={"example": "Transações"})
    backend_endpoint: str = Field(..., description="Local backend endpoint for the resource", json_schema_extra={"example": "/api/transactions"})
    pipeimob_endpoint: Optional[str] = Field(None, description="Confirmed Pipeimob endpoint (null if unconfirmed or divergent)", json_schema_extra={"example": None})
    status: str = Field(..., description="Status of the resource integration", json_schema_extra={"example": "implemented_pending_live_configuration"})
    implemented: bool = Field(..., description="Indicates if the resource integration is fully implemented", json_schema_extra={"example": True})
    validated: bool = Field(..., description="Indicates if the resource integration is validated with live credentials", json_schema_extra={"example": False})
    description: str = Field(..., description="Description of the resource", json_schema_extra={"example": "Transações comerciais do Pipeimob"})
    primary_key: str = Field(..., description="Primary key of the resource records", json_schema_extra={"example": "transacao_unique_id_pipeimob"})
    available_fields: List[str] = Field(..., description="List of available fields for extraction")
    supported_filters: List[str] = Field(..., description="List of supported query filters")
    filters_api_direct: List[str] = Field(..., description="List of filters processed directly at the Pipeimob CRM side")
    filters_local_backend: List[str] = Field(..., description="List of filters applied locally at the backend after fetch")
    pending_items: List[str] = Field(..., description="List of pending implementation items")

class CatalogResponse(BaseModel):
    api_version: str = Field(..., description="API version of the service", json_schema_extra={"example": "v2"})
    resources: List[ResourceCatalog] = Field(..., description="List of supported resources in the catalog")

class TransactionsDataPayload(BaseModel):
    count: int = Field(..., description="Count of returned transactions", json_schema_extra={"example": 60})
    transactions: List[dict] = Field(..., description="List of transaction objects")

class TransactionsListResponse(BaseModel):
    data_mode: str = Field(..., description="Active data mode: demo or live", json_schema_extra={"example": "demo"})
    source: str = Field(..., description="Source of data", json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., description="ISO-8601 UTC timestamp", json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: TransactionsDataPayload = Field(...)

class TransactionDetailResponse(BaseModel):
    data_mode: str = Field(..., description="Active data mode: demo or live", json_schema_extra={"example": "demo"})
    source: str = Field(..., description="Source of data", json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., description="ISO-8601 UTC timestamp", json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: dict = Field(..., description="Detailed transaction object")

class SummaryDataPayload(BaseModel):
    total_sales: float = Field(..., description="Sum of all contract values", json_schema_extra={"example": 323764790.0})
    total_commissions: float = Field(..., description="Sum of all commission values", json_schema_extra={"example": 17409771.0})
    avg_commission_rate: float = Field(..., description="Weighted average commission percentage", json_schema_extra={"example": 5.50})
    transaction_count: int = Field(..., description="Total count of deals/transactions", json_schema_extra={"example": 60})

class DashboardSummaryResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "demo"})
    source: str = Field(..., json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: SummaryDataPayload = Field(...)

class OriginMetric(BaseModel):
    origin: str = Field(..., description="Lead source name", json_schema_extra={"example": "PORTAL ZAP"})
    count: int = Field(..., description="Number of transactions from this origin", json_schema_extra={"example": 15})
    volume: float = Field(..., description="Total sales volume from this origin", json_schema_extra={"example": 12500000.0})

class OriginsDataPayload(BaseModel):
    origins: List[OriginMetric] = Field(...)

class DashboardOriginsResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "demo"})
    source: str = Field(..., json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: OriginsDataPayload = Field(...)

class StageMetric(BaseModel):
    stage: str = Field(..., description="Pipeline stage name", json_schema_extra={"example": "Fechamento"})
    count: int = Field(..., description="Number of transactions in this stage", json_schema_extra={"example": 12})
    volume: float = Field(..., description="Total sales volume in this stage", json_schema_extra={"example": 18500000.0})

class StagesDataPayload(BaseModel):
    stages: List[StageMetric] = Field(...)

class DashboardStagesResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "demo"})
    source: str = Field(..., json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: StagesDataPayload = Field(...)

class ManagerMetric(BaseModel):
    manager: str = Field(..., description="Name of the agent/manager", json_schema_extra={"example": "Corretor Alfa"})
    count: int = Field(..., description="Number of deals closed", json_schema_extra={"example": 10})
    volume: float = Field(..., description="Total sales volume closed", json_schema_extra={"example": 15210759.0})
    ticket_medio: float = Field(..., description="Average contract value", json_schema_extra={"example": 1521075.9})

class ManagersDataPayload(BaseModel):
    managers: List[ManagerMetric] = Field(...)

class DashboardManagersResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "demo"})
    source: str = Field(..., json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: ManagersDataPayload = Field(...)

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
    data_mode: str = Field(..., json_schema_extra={"example": "demo"})
    source: str = Field(..., json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: PaymentsDataPayload = Field(...)

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
    data_mode: str = Field(..., json_schema_extra={"example": "demo"})
    source: str = Field(..., json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: CommissionsDataPayload = Field(...)

class TimelineMetric(BaseModel):
    month: str = Field(..., description="Month/Year label", json_schema_extra={"example": "Jan/26"})
    volume: float = Field(..., description="Sales volume during the month", json_schema_extra={"example": 15000000.0})
    count: int = Field(..., description="Number of transactions during the month", json_schema_extra={"example": 12})

class TimelineDataPayload(BaseModel):
    timeline: List[TimelineMetric] = Field(...)

class DashboardTimelineResponse(BaseModel):
    data_mode: str = Field(..., json_schema_extra={"example": "demo"})
    source: str = Field(..., json_schema_extra={"example": "synthetic_mock"})
    generated_at: str = Field(..., json_schema_extra={"example": "2026-07-15T12:00:00Z"})
    data: TimelineDataPayload = Field(...)


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
    data_mode, conn_status = get_current_data_mode_and_connection()
    
    if data_mode == "unconfigured" or conn_status == "missing_credentials":
        status_str = "implemented_pending_live_configuration"
    elif data_mode == "live":
        status_str = "implemented_pending_live_validation"
    else:
        status_str = "implemented_demo_pending_live_validation"
        
    transactions_resource = ResourceCatalog(
        id="transactions",
        name="Transações",
        backend_endpoint="/api/transactions",
        pipeimob_endpoint="/api/v2/negocios/transacoes",
        status=status_str,
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
            "data_arquivamento_fim"
        ],
        filters_api_direct=[
            "data_inicio_criacao",
            "data_fim_criacao",
            "data_inicio_ccv",
            "data_fim_ccv",
            "data_arquivamento_inicio",
            "data_arquivamento_fim"
        ],
        filters_local_backend=[
            "agent",
            "category",
            "financing"
        ],
        pending_items=[
            "Confirmar propriedade que contém o token na resposta de autenticação",
            "Confirmar header de autenticação na API V2",
            "Confirmar se existe parâmetro na API para alterar o limite de registros por página (page_limit)",
            "Validar nova credencial real em produção (Live mode)",
            "Confirmar eventual suporte a filtro por etapa (etapa_atual) diretamente no Pipeimob"
        ]
    )
    
    return CatalogResponse(
        api_version="v2",
        resources=[transactions_resource]
    )

@app.get(
    "/api/transactions",
    response_model=TransactionsListResponse,
    summary="List Transactions",
    description="Returns list of transactions matching the specified query filters. Note: Agent, category, and financing filters are applied locally by the backend. Use PIPEIMOB_DATA_MODE=demo for synthetic mockup runs."
)
async def get_transactions(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
    )
    
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = TransactionsDataPayload(count=len(filtered), transactions=filtered)
    return meta

@app.get(
    "/api/transactions/{id}",
    response_model=TransactionDetailResponse,
    summary="Get Transaction by ID",
    description="Returns the details of a single transaction by ID (transacao_unique_id_pipeimob or codigo_contrato)."
)
async def get_transaction_by_id(
    id: str,
    response: Response
):
    mode, src, dataset = load_transactions_dataset()
    
    target_tx = None
    for tx in dataset:
        if tx.get("transacao_unique_id_pipeimob") == id or tx.get("codigo_contrato") == id:
            target_tx = tx
            break
            
    if not target_tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
        
    response.headers["X-Data-Mode"] = mode
    meta = get_metadata_wrapper(mode, src)
    meta["data"] = target_tx
    return meta

@app.get(
    "/api/dashboard/summary",
    response_model=DashboardSummaryResponse,
    summary="Get Dashboard BI Summary metrics",
    description="Computes total sales volume, commissions, weighted avg commission rate, and transaction count. Applied locally."
)
async def get_dashboard_summary(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
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
    summary="Get Buyer Origins distribution",
    description="Groups sales volume and transaction count by lead origin source. Applied locally."
)
async def get_dashboard_origins(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
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
    summary="Get Stages distribution",
    description="Groups sales volume and transaction count by CRM pipeline stage. Applied locally."
)
async def get_dashboard_stages(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
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
    summary="Get Manager Leaderboard",
    description="Computes leaderboard ranking of managers by sales volume, transaction count, and average ticket size. Applied locally."
)
async def get_dashboard_managers(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
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
    summary="Get Payment Methods and Financing distribution",
    description="Aggregates payment direct vs financing ratio, bank distributions, and detailed signals/methods volumes. Applied locally."
)
async def get_dashboard_payments(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
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
    summary="Get Commission detailed metrics",
    description="Returns aggregate commission values and individual contract commission details. Applied locally."
)
async def get_dashboard_commissions(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
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
    summary="Get Monthly Sales Timeline",
    description="Groups contract sales volume and count chronologically by month. Applied locally."
)
async def get_dashboard_timeline(
    response: Response,
    data_inicio_criacao: Optional[str] = Query(None),
    data_fim_criacao: Optional[str] = Query(None),
    data_inicio_ccv: Optional[str] = Query(None),
    data_fim_ccv: Optional[str] = Query(None),
    data_arquivamento_inicio: Optional[str] = Query(None),
    data_arquivamento_fim: Optional[str] = Query(None),
    agent: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    financing: Optional[bool] = Query(None)
):
    mode, src, dataset = load_transactions_dataset(
        data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim
    )
    filtered = get_filtered_transactions(
        dataset, mode, data_inicio_criacao, data_fim_criacao, data_inicio_ccv, data_fim_ccv, data_arquivamento_inicio, data_arquivamento_fim, agent, category, financing
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
