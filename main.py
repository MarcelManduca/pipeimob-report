import os
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI(
    title="Pipeimob Report API",
    description="Backend API for Pipeimob reports and Lovable integration",
    version="0.1.0",
)

# CORS Configuration
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins: List[str] = []

if allowed_origins_env:
    # Split comma-separated origins
    allowed_origins = [orig.strip() for orig in allowed_origins_env.split(",") if orig.strip()]

app_env = os.getenv("APP_ENV", "production").lower()

if app_env == "development":
    # Automatically add localhost endpoints for development
    dev_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    for orig in dev_origins:
        if orig not in allowed_origins:
            allowed_origins.append(orig)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic Schemas for OpenAPI documentation
class HealthResponse(BaseModel):
    status: str = Field(..., description="Status of the API service", json_schema_extra={"example": "ok"})
    service: str = Field(..., description="Name of the service", json_schema_extra={"example": "pipeimob-report"})
    version: str = Field(..., description="Version of the service", json_schema_extra={"example": "0.1.0"})
    api_version: str = Field(..., description="API version of the service", json_schema_extra={"example": "v2"})
    pipeimob_connection: str = Field(..., description="Connection status to Pipeimob CRM", json_schema_extra={"example": "pending"})
    timestamp: str = Field(..., description="Current timestamp in UTC ISO-8601 format", json_schema_extra={"example": "2026-01-01T00:00:00Z"})

class ResourceCatalog(BaseModel):
    id: str = Field(..., description="Unique resource ID", json_schema_extra={"example": "transactions"})
    name: str = Field(..., description="Resource name", json_schema_extra={"example": "Transações"})
    backend_endpoint: str = Field(..., description="Local backend endpoint for the resource", json_schema_extra={"example": "/api/transactions"})
    pipeimob_endpoint: Optional[str] = Field(None, description="Confirmed Pipeimob endpoint (null if unconfirmed or divergent)", json_schema_extra={"example": None})
    status: str = Field(..., description="Status of the resource integration", json_schema_extra={"example": "pending_auth_confirmation"})
    implemented: bool = Field(..., description="Indicates if the resource integration is fully implemented", json_schema_extra={"example": False})
    validated: bool = Field(..., description="Indicates if the resource integration is validated with live credentials", json_schema_extra={"example": False})
    description: str = Field(..., description="Description of the resource", json_schema_extra={"example": "Transações comerciais do Pipeimob"})
    primary_key: str = Field(..., description="Primary key of the resource records", json_schema_extra={"example": "transacao_unique_id_pipeimob"})
    available_fields: List[str] = Field(..., description="List of available fields for extraction")
    supported_filters: List[str] = Field(..., description="List of supported query filters")
    pending_items: List[str] = Field(..., description="List of pending implementation items")

class CatalogResponse(BaseModel):
    api_version: str = Field(..., description="API version of the service", json_schema_extra={"example": "v2"})
    resources: List[ResourceCatalog] = Field(..., description="List of supported resources in the catalog")

@app.get(
    "/api/health",
    response_model=HealthResponse,
    summary="Get Service Health Status",
    description="Returns HTTP 200 and health info when service is running. Does not perform auth or external network calls."
)
async def get_health():
    # Return current UTC time in ISO-8601 format ending with Z
    timestamp_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return HealthResponse(
        status="ok",
        service="pipeimob-report",
        version="0.1.0",
        api_version="v2",
        pipeimob_connection="pending",
        timestamp=timestamp_utc
    )

@app.get(
    "/api/catalog",
    response_model=CatalogResponse,
    summary="Get Resource Catalog",
    description="Returns the integration roadmap status, available fields, filters and pending items for Pipeimob resources."
)
async def get_catalog():
    # Null pipeimob_endpoint due to divergence: /api/v2/negocios/transacoes vs /api/v2/transacoes
    transactions_resource = ResourceCatalog(
        id="transactions",
        name="Transações",
        backend_endpoint="/api/transactions",
        pipeimob_endpoint=None,
        status="pending_auth_confirmation",
        implemented=False,
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
        pending_items=[
            "Confirmar endpoint definitivo de transações",
            "Confirmar propriedade que contém o token",
            "Confirmar header de autenticação",
            "Confirmar parâmetro de paginação",
            "Validar nova credencial"
        ]
    )
    
    return CatalogResponse(
        api_version="v2",
        resources=[transactions_resource]
    )
