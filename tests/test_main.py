import os
import sys
import json
import asyncio
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Force development environment for local localhost CORS tests
os.environ["APP_ENV"] = "development"
os.environ["ALLOWED_ORIGINS"] = "https://lovable-test-origin.app"
os.environ["SUPABASE_ISSUER"] = "https://mock.supabase.co/auth/v1"
os.environ["SUPABASE_JWT_AUDIENCE"] = "authenticated"

import jwt
import time

def create_mock_jwt(
    email="user@gralhaimoveis.com.br",
    expired=False,
    iss="https://mock.supabase.co/auth/v1",
    aud="authenticated",
    role="authenticated",
    sub="mock_user_123",
    alg="HS256",
    headers={"kid": "mock_kid"}
):
    payload = {
        "email": email,
        "sub": sub,
        "aud": aud,
        "role": role,
        "iss": iss,
        "exp": time.time() - 3600 if expired else time.time() + 3600
    }
    # Filter out None values to test missing claims
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, "secret", algorithm=alg, headers=headers)

mock_token = create_mock_jwt()

from mock_data import MOCK_TRANSACTIONS
from main import app, dashboard_cache
import pytest

@pytest.fixture(autouse=True)
def clear_dashboard_cache():
    dashboard_cache.clear()

client = TestClient(app, headers={"Authorization": f"Bearer {mock_token}"})

def test_app_starts_without_credentials():
    assert app is not None

def test_get_health_status_code_200():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "pipeimob-report"
    assert data["version"] == "0.1.0"
    assert data["api_version"] == "v2"
    assert data["pipeimob_connection"] == "not_tested"  # "not_tested" in demo mode
    assert data["data_mode"] == "demo"

def test_get_health_no_secrets_exposed():
    response = client.get("/api/health")
    data_str = response.text
    assert "key" not in data_str.lower()
    assert "secret" not in data_str.lower()
    assert "token" not in data_str.lower()
    assert "env" not in data_str.lower()

def test_get_health_timestamp_valid_utc():
    response = client.get("/api/health")
    data = response.json()
    timestamp_str = data["timestamp"]
    assert timestamp_str.endswith("Z")
    parsed_dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    assert parsed_dt.tzinfo == timezone.utc

def test_get_catalog_returns_transactions_resource():
    response = client.get("/api/catalog")
    assert response.status_code == 200
    data = response.json()
    assert data["api_version"] == "v2"
    
    resources = data["resources"]
    assert len(resources) == 1
    
    resource = resources[0]
    assert resource["id"] == "transactions"
    assert resource["name"] == "Transações"
    assert resource["backend_endpoint"] == "/api/transactions"
    assert resource["pipeimob_endpoint"] == "/api/v2/negocios/transacoes"
    assert resource["status"] == "implemented_pending_live_validation"
    assert resource["implemented"] is True
    assert resource["validated"] is False
    assert resource["primary_key"] == "transacao_unique_id_pipeimob"

def test_get_catalog_contains_expected_fields():
    response = client.get("/api/catalog")
    resource = response.json()["resources"][0]
    
    expected_fields = [
        "transacao_unique_id_pipeimob", "codigo_contrato", "codigo_imovel", 
        "etapa_atual", "data_contrato", "data_inicio_venda", "valor_contrato", 
        "total_comissao", "comissao_imobiliaria", "agente_gestor", 
        "midia_origem_compradores", "forma_pagamento", "comissionados", "clientes"
    ]
    for field in expected_fields:
        assert field in resource["available_fields"]

def test_get_catalog_contains_expected_filters():
    response = client.get("/api/catalog")
    resource = response.json()["resources"][0]
    
    expected_filters = ["data_inicio_criacao"]
    for filter_name in expected_filters:
        assert filter_name in resource["supported_filters"]

def test_cors_authorized_origin():
    headers = {"Origin": "https://lovable-test-origin.app"}
    response = client.get("/api/health", headers=headers)
    assert response.headers.get("access-control-allow-origin") == "https://lovable-test-origin.app"

def test_cors_authorized_localhost_in_dev():
    headers = {"Origin": "http://localhost:5173"}
    response = client.get("/api/health", headers=headers)
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

def test_cors_unauthorized_origin():
    headers = {"Origin": "https://unauthorized-domain.com"}
    response = client.get("/api/health", headers=headers)
    assert "access-control-allow-origin" not in response.headers

def test_cors_preflight_options():
    headers = {
        "Origin": "https://lovable-test-origin.app",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Content-Type",
    }
    response = client.options("/api/health", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://lovable-test-origin.app"
    assert "GET" in response.headers.get("access-control-allow-methods", "")

def test_demo_data_anonymization_and_purity():
    # Assert that no real manager/agency names or properties from real sheets remain in the mock dataset
    real_names = ["Raphael", "Carvalho", "Vanessa", "Cavedon", "Gralha", "Manduca", "Michele", "Maitê", "Yakabi"]
    for tx in MOCK_TRANSACTIONS:
        # Check managers
        assert not any(name.lower() in tx["agente_gestor"].lower() for name in real_names)
        # Check imobiliária name
        assert "gralha" not in tx["imobiliária"].lower()
        # Check buyer/seller clients
        for client_obj in tx["clientes"]:
            assert not any(name.lower() in client_obj["nome"].lower() for name in real_names)

def test_get_transactions_demo_metadata_and_headers():
    # Set demo mode explicitly
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/transactions")
    assert response.status_code == 200
    assert response.headers.get("X-Data-Mode") == "demo"
    
    data = response.json()
    assert data["data_mode"] == "demo"
    assert data["source"] == "synthetic_mock"
    assert "generated_at" in data
    
    # Check wrapped count
    payload = data["data"]
    assert payload["count"] == 60
    assert len(payload["transactions"]) == 60

def test_get_transactions_with_filters():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/transactions?agent=Corretor Alfa")
    assert response.status_code == 200
    payload = response.json()["data"]
    for tx in payload["transactions"]:
        assert "Corretor Alfa" in tx["agente_gestor"]
        
    # Test period filter (data_inicio_criacao)
    response_date = client.get("/api/transactions?data_inicio_criacao=2025-01-01")
    assert response_date.status_code == 200
    payload_date = response_date.json()["data"]
    for tx in payload_date["transactions"]:
        tx_date = tx.get("data_inicio_venda") or tx.get("data_contrato") or ""
        assert tx_date >= "2025-01-01"

def test_get_transaction_by_id():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/transactions/tx_demo_101")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "demo"
    assert data["data"]["transacao_unique_id_pipeimob"] == "tx_demo_101"

def test_get_dashboard_summary_metadata():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "demo"
    
    # Headers in TestClient are lowercased
    assert response.headers.get("x-data-mode") == "demo"
    
    payload = data["data"]
    assert payload["total_sales"] > 0
    assert payload["transaction_count"] == 60

def test_get_dashboard_origins():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/dashboard/origins")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "demo"
    assert len(data["data"]["origins"]) > 0

def test_get_dashboard_stages():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/dashboard/stages")
    assert response.status_code == 200
    assert response.json()["data_mode"] == "demo"

def test_get_dashboard_managers():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/dashboard/managers")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "demo"
    assert "Corretor" in data["data"]["managers"][0]["manager"]

def test_get_dashboard_payments():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/dashboard/payments")
    assert response.status_code == 200
    assert response.json()["data_mode"] == "demo"

def test_get_dashboard_commissions():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/dashboard/commissions")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "demo"
    assert data["data"]["total_commissions"] > 0

def test_get_dashboard_timeline():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/dashboard/timeline")
    assert response.status_code == 200
    assert response.json()["data_mode"] == "demo"

def test_production_unconfigured_without_mode():
    # Production without mode environment variable configured
    os.environ["APP_ENV"] = "production"
    os.environ.pop("PIPEIMOB_DATA_MODE", None)
    
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "unconfigured"
    assert data["pipeimob_connection"] == "pending_configuration"
    # Verify production does not automatically assume live mode
    assert data["data_mode"] != "live"
    os.environ["APP_ENV"] = "development"

def test_live_without_credentials_missing_credentials():
    os.environ["APP_ENV"] = "production"
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ.pop("PIPEIMOB_API_KEY", None)
    os.environ.pop("PIPEIMOB_SECRET_KEY", None)
    
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "live"
    assert data["pipeimob_connection"] == "missing_credentials"
    os.environ["APP_ENV"] = "development"

def test_live_with_credentials_configured():
    os.environ["APP_ENV"] = "production"
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "real_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "real_secret"
    
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["data_mode"] == "live"
    assert data["pipeimob_connection"] == "configured"
    os.environ["APP_ENV"] = "development"

def test_unconfigured_endpoints_return_503():
    from main import verify_backend_api_key
    app.dependency_overrides[verify_backend_api_key] = lambda: {"email": "test@gralhaimoveis.com.br", "sub": "test-user-id"}
    os.environ["APP_ENV"] = "production"
    os.environ.pop("PIPEIMOB_DATA_MODE", None)
    try:
        # Endpoints must fail with 503 while unconfigured, never returning demo data silently
        response = client.get("/api/transactions")
        assert response.status_code == 503
        assert "Configuration pending" in response.json()["detail"]
        
        response = client.get("/api/dashboard/summary")
        assert response.status_code == 503
        assert "Configuration pending" in response.json()["detail"]
    finally:
        os.environ["APP_ENV"] = "development"
        app.dependency_overrides.clear()

def test_six_filters_appear_in_catalog():
    response = client.get("/api/catalog")
    assert response.status_code == 200
    resource = response.json()["resources"][0]
    
    expected_filters = [
        "data_inicio_criacao",
        "data_fim_criacao",
        "data_inicio_ccv",
        "data_fim_ccv",
        "data_arquivamento_inicio",
        "data_arquivamento_fim",
        "codigo_imovel",
        "codigo_contrato",
        "transacao_unique_id"
    ]
    for filter_name in expected_filters:
        assert filter_name in resource["supported_filters"]
        
    assert resource["filters_api_direct"] == [
        "data_inicio_criacao",
        "data_fim_criacao",
        "data_inicio_ccv",
        "data_fim_ccv",
        "data_arquivamento_inicio",
        "data_arquivamento_fim",
        "codigo_imovel",
        "codigo_contrato",
        "transacao_unique_id"
    ]
    assert resource["filters_local_backend"] == [
        "agent",
        "category",
        "financing",
        "etapa_atual"
    ]
    assert resource["pagination_parameters"] == [
        "pagina"
    ]

def test_catalog_status_states():
    # 1. Demo Mode
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_pending_live_validation"
    
    # 2. Live Mode (no credentials)
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ.pop("PIPEIMOB_API_KEY", None)
    os.environ.pop("PIPEIMOB_SECRET_KEY", None)
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_pending_live_validation"
    
    # 3. Unconfigured Mode (production)
    os.environ["APP_ENV"] = "production"
    os.environ.pop("PIPEIMOB_DATA_MODE", None)
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_pending_live_validation"
    os.environ["APP_ENV"] = "development"
    
    # 4. Live Mode (with credentials configured)
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_pending_live_validation"

from unittest.mock import patch, MagicMock

def test_live_mode_without_credentials_returns_error():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ.pop("PIPEIMOB_API_KEY", None)
    os.environ.pop("PIPEIMOB_SECRET_KEY", None)
    
    # Must supply a direct filter so it doesn't fail on filter check first
    response = client.get("/api/transactions?data_inicio_criacao=2026-01-01")
    assert response.status_code == 503
    assert "credentials are not configured" in response.json()["detail"]

def test_headers_credentials_are_ignored():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ.pop("PIPEIMOB_API_KEY", None)
    os.environ.pop("PIPEIMOB_SECRET_KEY", None)
    
    headers = {
        "X-API-Key": "client_supplied_key",
        "X-Secret-Key": "client_supplied_secret"
    }
    response = client.get("/api/transactions?data_inicio_criacao=2026-01-01", headers=headers)
    assert response.status_code == 503
    assert "credentials are not configured" in response.json()["detail"]

def test_live_mode_failure_does_not_return_mock():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    response = client.get("/api/transactions?data_inicio_criacao=2026-01-01")
    assert response.status_code == 503
    assert "Failed to authenticate" in response.json()["detail"] or "Authentication payload" in response.json()["detail"]

def test_live_mode_missing_direct_filter_returns_400():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    response = client.get("/api/transactions")
    assert response.status_code == 400
    assert "At least one direct filter parameter is required" in response.json()["detail"]

@patch("urllib.request.urlopen")
def test_jwt_auth_extraction_and_caching(mock_urlopen):
    import main
    main.token_cache.access_token = None
    main.token_cache.expires_at = None

    # Mock JWT authentication response
    mock_auth_response = MagicMock()
    mock_auth_response.__enter__.return_value = mock_auth_response
    mock_auth_response.read.return_value = json.dumps({
        "success": True,
        "status_code": 200,
        "message": "Autenticação realizada com sucesso",
        "data": {
            "access_token": "mocked_jwt_token_12345",
            "token_type": "Bearer",
            "expires_in": 3600
        }
    }).encode("utf-8")
    
    # Mock transactions list response with meta.pagination
    mock_txs_response = MagicMock()
    mock_txs_response.__enter__.return_value = mock_txs_response
    mock_txs_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "transacoes": [
                {
                    "transacao_unique_id_pipeimob": "tx_mock_1",
                    "codigo_contrato": "CONTRATO-MOCK-1",
                    "total_comissao": 10000.0,
                    "comissionados": [
                        {
                            "comissionado_imobiliária": True,
                            "comissionado_valor": 6000.0
                        },
                        {
                            "comissionado_imobiliária": False,
                            "comissionado_valor": 4000.0
                        }
                    ]
                }
            ]
        },
        "meta": {
            "pagination": {
                "total_pages": 1
            }
        }
    }).encode("utf-8")
    
    # urlopen returns auth first, then txs
    mock_urlopen.side_effect = [mock_auth_response, mock_txs_response]
    
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    response = client.get("/api/transactions?data_inicio_criacao=2026-01-01")
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["data_mode"] == "live"
    assert len(res_data["data"]["transactions"]) == 1
    assert res_data["data"]["transactions"][0]["transacao_unique_id_pipeimob"] == "tx_mock_1"
    
    # Verify comissao_imobiliaria calculation
    assert res_data["data"]["transactions"][0]["comissao_imobiliaria"] == 6000.0

@patch("urllib.request.urlopen")
def test_401_retry_once_and_prevent_loop(mock_urlopen):
    import main
    main.token_cache.access_token = None
    main.token_cache.expires_at = None

    # Mock auth response (returns token)
    mock_auth_response = MagicMock()
    mock_auth_response.__enter__.return_value = mock_auth_response
    mock_auth_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "access_token": "mocked_jwt_token_401",
            "token_type": "Bearer",
            "expires_in": 3600
        }
    }).encode("utf-8")
    
    # Mock HTTP 401 error for transactions call
    from urllib.error import HTTPError
    mock_401_err = HTTPError("http://api.pipeimob.com.br/api/v2/negocios/transacoes", 401, "Unauthorized", {}, None)
    
    # Mock final transactions success response with meta.pagination
    mock_txs_response = MagicMock()
    mock_txs_response.__enter__.return_value = mock_txs_response
    mock_txs_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "transacoes": []
        },
        "meta": {
            "pagination": {
                "total_pages": 1
            }
        }
    }).encode("utf-8")
    
    mock_urlopen.side_effect = [mock_auth_response, mock_401_err, mock_auth_response, mock_txs_response]
    
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    response = client.get("/api/transactions?data_inicio_criacao=2026-01-01")
    assert response.status_code == 200, response.json()
    assert response.json()["data_mode"] == "live"

@patch("urllib.request.urlopen")
def test_401_retry_once_and_prevent_loop(mock_urlopen):
    import main
    main.token_cache.access_token = None
    main.token_cache.expires_at = None

    # Mock auth response (returns token)
    mock_auth_response = MagicMock()
    mock_auth_response.__enter__.return_value = mock_auth_response
    mock_auth_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "access_token": "mocked_jwt_token_401",
            "token_type": "Bearer",
            "expires_in": 3600
        }
    }).encode("utf-8")
    
    # Mock HTTP 401 error for transactions call
    from urllib.error import HTTPError
    mock_401_err = HTTPError("http://api.pipeimob.com.br/api/v2/negocios/transacoes", 401, "Unauthorized", {}, None)
    
    # Mock final transactions success response with meta.pagination
    mock_txs_response = MagicMock()
    mock_txs_response.__enter__.return_value = mock_txs_response
    mock_txs_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "transacoes": [
                {
                    "transacao_unique_id_pipeimob": "tx_mock_retry_1",
                    "codigo_contrato": "CONTRATO-MOCK-RETRY-1",
                    "total_comissao": 10000.0,
                    "comissionados": []
                }
            ]
        },
        "meta": {
            "pagination": {
                "total_pages": 1
            }
        }
    }).encode("utf-8")
    
    mock_urlopen.side_effect = [mock_auth_response, mock_401_err, mock_auth_response, mock_txs_response]
    
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    response = client.get("/api/transactions?data_inicio_criacao=2026-01-01")
    assert response.status_code == 200, response.json()
    assert response.json()["data_mode"] == "live"

@patch("urllib.request.urlopen")
def test_data_meta_pagination_fallback(mock_urlopen):
    import main
    main.token_cache.access_token = None
    main.token_cache.expires_at = None

    # Mock auth response
    mock_auth_response = MagicMock()
    mock_auth_response.__enter__.return_value = mock_auth_response
    mock_auth_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "access_token": "mocked_jwt_token_pagination",
            "token_type": "Bearer",
            "expires_in": 3600
        }
    }).encode("utf-8")
    
    # Mock transactions response with data.meta.pagination (nested pagination)
    mock_txs_response = MagicMock()
    mock_txs_response.__enter__.return_value = mock_txs_response
    mock_txs_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "transacoes": [
                {
                    "transacao_unique_id_pipeimob": "tx_mock_nested_1",
                    "codigo_contrato": "CONTRATO-MOCK-NESTED-1",
                    "total_comissao": 10000.0,
                    "comissionados": []
                }
            ],
            "meta": {
                "pagination": {
                    "total_pages": 1
                }
            }
        }
    }).encode("utf-8")
    
    mock_urlopen.side_effect = [mock_auth_response, mock_txs_response]
    
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    response = client.get("/api/transactions?data_inicio_criacao=2026-01-01")
    assert response.status_code == 200, response.json()
    assert response.json()["data_mode"] == "live"

def test_openapi_includes_new_endpoints_and_schemas():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    openapi_data = response.json()
    
    paths = openapi_data["paths"]
    assert "/api/transactions" in paths
    assert "/api/dashboard/summary" in paths
    
    tx_get_params = paths["/api/transactions"]["get"].get("parameters", [])
    param_names = [p["name"].lower() for p in tx_get_params]
    assert "x-api-key" not in param_names
    assert "x-secret-key" not in param_names
    
    # Verify new filters appear in Swagger parameters list
    assert "codigo_imovel" in param_names
    assert "codigo_contrato" in param_names
    assert "transacao_unique_id" in param_names
    assert "etapa_atual" in param_names
    assert "pagina" in param_names
    
    # Check limit-related parameters are absent
    assert "limit" not in param_names
    assert "page_limit" not in param_names
    assert "page_size" not in param_names
    
    schemas = openapi_data["components"]["schemas"]
    assert "TransactionsListResponse" in schemas
    assert "DashboardSummaryResponse" in schemas
    assert "IntegrationUnavailableResponse" in schemas

    # Verify that all 9 data/dashboard endpoints have 503 response documented in OpenAPI
    data_endpoints = [
        "/api/transactions",
        "/api/transactions/{id}",
        "/api/dashboard/summary",
        "/api/dashboard/origins",
        "/api/dashboard/stages",
        "/api/dashboard/managers",
        "/api/dashboard/payments",
        "/api/dashboard/commissions",
        "/api/dashboard/timeline"
    ]
    for path in data_endpoints:
        assert path in paths
        assert "503" in paths[path]["get"]["responses"]
        
    # Verify main examples do not use demo mode as production default
    tx_schema = schemas["TransactionsListResponse"]
    assert tx_schema["properties"]["data_mode"]["example"] == "live"
    assert tx_schema["properties"]["source"]["example"] == "pipeimob_api_v2"

    health_schema = schemas["HealthResponse"]
    assert health_schema["properties"]["data_mode"]["example"] == "unconfigured"
    assert health_schema["properties"]["pipeimob_connection"]["example"] == "pending_configuration"
    # 503 errors do not leak secrets
    os.environ["APP_ENV"] = "production"
    os.environ.pop("PIPEIMOB_DATA_MODE", None)
    from main import verify_backend_api_key
    app.dependency_overrides[verify_backend_api_key] = lambda: {"email": "test@gralhaimoveis.com.br", "sub": "test-user-id"}
    try:
        err_res = client.get(
            "/api/transactions?data_inicio_criacao=2026-01-01"
        )
        assert err_res.status_code == 503
        err_body = err_res.json()
        for val in err_body.values():
            val_str = str(val).lower()
            assert "api_key" not in val_str
            assert "secret_key" not in val_str
            assert "token" not in val_str
    finally:
        os.environ["APP_ENV"] = "development"
        app.dependency_overrides.clear()

def test_live_mode_only_pagina_returns_400():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    # Query with ONLY pagina (should fail as it doesn't satisfy direct filter requirement on its own)
    response = client.get("/api/transactions?pagina=1")
    assert response.status_code == 400
    assert "At least one direct filter parameter is required" in response.json()["detail"]

@patch("urllib.request.urlopen")
def test_live_mode_pagina_with_direct_filter_is_allowed(mock_urlopen):
    import main
    main.token_cache.access_token = None
    main.token_cache.expires_at = None

    # Mock auth response
    mock_auth_response = MagicMock()
    mock_auth_response.__enter__.return_value = mock_auth_response
    mock_auth_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "access_token": "mocked_jwt_token_123",
            "token_type": "Bearer",
            "expires_in": 3600
        }
    }).encode("utf-8")
    
    # Mock transactions list response
    mock_txs_response = MagicMock()
    mock_txs_response.__enter__.return_value = mock_txs_response
    mock_txs_response.read.return_value = json.dumps({
        "success": True,
        "data": {
            "transacoes": [
                {
                    "transacao_unique_id_pipeimob": "tx_mock_1",
                    "codigo_contrato": "CONTRATO-MOCK-1",
                    "total_comissao": 10000.0,
                    "comissionados": []
                }
            ]
        },
        "meta": {
            "pagination": {
                "total_pages": 1
            }
        }
    }).encode("utf-8")
    
    mock_urlopen.side_effect = [mock_auth_response, mock_txs_response]
    
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    # Query with direct filter AND pagina
    response = client.get("/api/transactions?data_inicio_ccv=2026-07-01&pagina=1")
    assert response.status_code == 200

def test_public_endpoints_accessible_without_token():
    unauth_client = TestClient(app)
    # GET /api/health is public
    res_health = unauth_client.get("/api/health")
    assert res_health.status_code == 200
    
    # GET /api/catalog is public
    res_catalog = unauth_client.get("/api/catalog")
    assert res_catalog.status_code == 200


def test_protected_endpoints_auth_failures():
    unauth_client = TestClient(app)
    endpoints = [
        "/api/transactions",
        "/api/transactions/some_id",
        "/api/dashboard/summary",
        "/api/dashboard/origins",
        "/api/dashboard/stages",
        "/api/dashboard/managers",
        "/api/dashboard/payments",
        "/api/dashboard/commissions",
        "/api/dashboard/timeline"
    ]
    
    # 1. Missing Authorization header -> HTTP 401 (Authentication required)
    for ep in endpoints:
        res = unauth_client.get(ep)
        assert res.status_code == 401
        body = res.json()
        assert body["detail"] == "Authentication required."
        assert body["error_code"] == "authentication_required"

    # 2. Invalid/malformed token header -> HTTP 401 (Invalid or expired access token)
    bad_token_client = TestClient(app, headers={"Authorization": "Bearer bad-token-format"})
    for ep in endpoints:
        res = bad_token_client.get(ep)
        assert res.status_code == 401
        body = res.json()
        assert body["detail"] == "Invalid or expired access token."
        assert body["error_code"] == "invalid_access_token"

    # 3. Expired token -> HTTP 401 (Invalid or expired access token)
    expired_token = create_mock_jwt(expired=True)
    expired_client = TestClient(app, headers={"Authorization": f"Bearer {expired_token}"})
    for ep in endpoints:
        res = expired_client.get(ep)
        assert res.status_code == 401
        body = res.json()
        assert body["detail"] == "Invalid or expired access token."
        assert body["error_code"] == "invalid_access_token"


def test_user_authorization_allowlists():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    # 1. User email/domain outside allowlist -> HTTP 403 Forbidden
    unauthorized_token = create_mock_jwt(email="hacker@gmail.com")
    unauth_user_client = TestClient(app, headers={"Authorization": f"Bearer {unauthorized_token}"})
    
    # Temporarily set allowed env variables to gralhaimoveis.com.br only (which doesn't match gmail.com)
    os.environ["ALLOWED_EMAIL_DOMAINS"] = "gralhaimoveis.com.br"
    os.environ["ALLOWED_USER_EMAILS"] = ""
    
    res = unauth_user_client.get("/api/dashboard/summary")
    assert res.status_code == 403
    body = res.json()
    assert body["detail"] == "User is not authorized to access this resource."
    assert body["error_code"] == "forbidden"

    # 2. Domain matches ALLOWED_EMAIL_DOMAINS -> HTTP 200 OK
    authorized_token = create_mock_jwt(email="corretor@gralhaimoveis.com.br")
    auth_user_client = TestClient(app, headers={"Authorization": f"Bearer {authorized_token}"})
    res_ok = auth_user_client.get("/api/dashboard/summary")
    assert res_ok.status_code == 200

    # 3. Email specifically listed in ALLOWED_USER_EMAILS -> HTTP 200 OK
    special_token = create_mock_jwt(email="guest-external@example.com")
    special_client = TestClient(app, headers={"Authorization": f"Bearer {special_token}"})
    os.environ["ALLOWED_USER_EMAILS"] = "guest-external@example.com,other@domain.com"
    res_special = special_client.get("/api/dashboard/summary")
    assert res_special.status_code == 200


def test_invalid_header_rejection():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    unauth_client = TestClient(app)
    
    # Passing unmapped headers -> HTTP 401 Unauthorized now
    res = unauth_client.get("/api/dashboard/summary", headers={"X-Header-Test": "some_value"})
    assert res.status_code == 401


def test_privacy_compliance_on_public_responses():
    # Set demo data mode to use mock transactions
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    os.environ["EXPOSE_RAW_TRANSACTIONS"] = "false"
    
    # 1. Fetch transactions list
    response = client.get("/api/transactions")
    assert response.status_code == 200
    data = response.json()
    
    # Assert that EXPOSE_RAW_TRANSACTIONS defaults to false and payload is sanitized
    assert os.getenv("EXPOSE_RAW_TRANSACTIONS", "false").lower() == "false"
    
    # Let's perform recursive checks on all keys and values in the response JSON
    sensitive_keys = {
        "cpf", "cnpj", "celular", "email", "link_acesso", "documentos", 
        "cobrancas_bancarias", "url", "token", "api_key", "secret_key"
    }
    
    def verify_no_sensitive_data(node):
        if isinstance(node, dict):
            for k, v in node.items():
                k_lower = k.lower()
                for sensitive in sensitive_keys:
                    assert sensitive not in k_lower, f"Sensitive key '{k}' found in response!"
                verify_no_sensitive_data(v)
        elif isinstance(node, list):
            for item in node:
                verify_no_sensitive_data(item)
        elif isinstance(node, str):
            val_lower = node.lower()
            # Assert that no value contains sensitive-looking substrings like typical emails or keys in plain text
            for sensitive in ["@gralha", "secret_key", "api_key", "bearer"]:
                # Ignore mock emails if any,
                # but assert actual spreadsheet PII does not exist.
                assert sensitive not in val_lower, f"Sensitive substring '{sensitive}' found in string value: {node}"

    verify_no_sensitive_data(data)
    
    # 2. Fetch single transaction details
    tx_id = data["data"]["transactions"][0]["transacao_unique_id_pipeimob"]
    detail_res = client.get(f"/api/transactions/{tx_id}")
    assert detail_res.status_code == 200
    verify_no_sensitive_data(detail_res.json())

    # 3. Check all dashboard endpoints as well
    dashboard_endpoints = [
        "/api/dashboard/summary",
        "/api/dashboard/origins",
        "/api/dashboard/stages",
        "/api/dashboard/managers",
        "/api/dashboard/payments",
        "/api/dashboard/commissions",
        "/api/dashboard/timeline"
    ]
    for ep in dashboard_endpoints:
        res = client.get(ep)
        assert res.status_code == 200
        verify_no_sensitive_data(res.json())


def test_expose_raw_transactions_flag():
    # If EXPOSE_RAW_TRANSACTIONS is set to true, raw transactions (including raw buyers/sellers lists) are returned
    os.environ["EXPOSE_RAW_TRANSACTIONS"] = "true"
    try:
        os.environ["PIPEIMOB_DATA_MODE"] = "demo"
        response = client.get("/api/transactions")
        assert response.status_code == 200
        txs = response.json()["data"]["transactions"]
        if txs:
            # Raw transaction should expose full mock compradores list (represented by 'clientes' list in mock data)
            # whereas sanitized transaction only has counts.
            first_tx = txs[0]
            assert "clientes" in first_tx
            assert isinstance(first_tx["clientes"], list)
    finally:
        os.environ["EXPOSE_RAW_TRANSACTIONS"] = "false"

def test_supabase_jwt_validation_claims_and_unsafe_jwks():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    
    # 1. Incorrect Issuer (expected: https://mock.supabase.co/auth/v1) -> HTTP 401
    bad_iss_token = create_mock_jwt(iss="https://hacker-issuer.supabase.co/auth/v1")
    bad_iss_client = TestClient(app, headers={"Authorization": f"Bearer {bad_iss_token}"})
    res = bad_iss_client.get("/api/dashboard/summary")
    assert res.status_code == 401
    assert "Invalid or expired access token." in res.json()["detail"]
    
    # 2. Incorrect Audience (expected: authenticated) -> HTTP 401
    bad_aud_token = create_mock_jwt(aud="hacker-audience")
    bad_aud_client = TestClient(app, headers={"Authorization": f"Bearer {bad_aud_token}"})
    res = bad_aud_client.get("/api/dashboard/summary")
    assert res.status_code == 401
    assert "Invalid or expired access token." in res.json()["detail"]
    
    # 3. JWKS empty / unavailable -> HTTP 503 Service Unavailable
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        jwks_client = TestClient(app, headers={"Authorization": f"Bearer {mock_token}"})
        res = jwks_client.get("/api/dashboard/summary")
        assert res.status_code == 503
        assert res.json()["detail"] == "Supabase project does not expose asymmetric JWT signing keys."
        assert res.json()["error_code"] == "supabase_jwks_unavailable"
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

    # 4. Missing sub claim -> HTTP 401
    no_sub_token = create_mock_jwt(sub=None)
    no_sub_client = TestClient(app, headers={"Authorization": f"Bearer {no_sub_token}"})
    res = no_sub_client.get("/api/dashboard/summary")
    assert res.status_code == 401
    assert "Invalid or expired access token." in res.json()["detail"]

    # 5. Missing email claim -> HTTP 401
    no_email_token = create_mock_jwt(email=None)
    no_email_client = TestClient(app, headers={"Authorization": f"Bearer {no_email_token}"})
    res = no_email_client.get("/api/dashboard/summary")
    assert res.status_code == 401
    assert "Invalid or expired access token." in res.json()["detail"]

    # 6. Incorrect role (expected: authenticated) -> HTTP 401
    bad_role_token = create_mock_jwt(role="guest")
    bad_role_client = TestClient(app, headers={"Authorization": f"Bearer {bad_role_token}"})
    res = bad_role_client.get("/api/dashboard/summary")
    assert res.status_code == 401
    assert "Invalid or expired access token." in res.json()["detail"]

@patch("main.get_jwk_client")
def test_disallowed_algorithm_returns_401(mock_get_jwk_client):
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    try:
        mock_jwk_client = MagicMock()
        mock_key = MagicMock()
        mock_key.key = "dummy_public_key"
        mock_jwk_client.get_signing_key_from_jwt.return_value = mock_key
        mock_get_jwk_client.return_value = mock_jwk_client
        
        # Create token signed with HS256
        hs256_token = create_mock_jwt(alg="HS256")
        hs256_client = TestClient(app, headers={"Authorization": f"Bearer {hs256_token}"})
        
        res = hs256_client.get("/api/dashboard/summary")
        assert res.status_code == 401
        assert "Invalid or expired access token." in res.json()["detail"]
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

def test_antifallback_live_mode_uses_mock_fails():
    import pytest
    from fastapi import HTTPException
    from main import validate_dataset_origin
    from mock_data import MOCK_TRANSACTIONS
    with pytest.raises(HTTPException) as exc_info:
        validate_dataset_origin("live", "pipeimob_api_v2", MOCK_TRANSACTIONS)
    assert exc_info.value.status_code == 500
    assert "Mock data detected in live dataset" in exc_info.value.detail

def test_antifallback_source_mismatch_fails():
    import pytest
    from fastapi import HTTPException
    from main import validate_dataset_origin
    # If mode is live but source is synthetic_mock, should fail
    with pytest.raises(HTTPException) as exc_info:
        validate_dataset_origin("live", "synthetic_mock", [])
    assert exc_info.value.status_code == 500
    assert "Data source mismatch" in exc_info.value.detail

def test_antifallback_api_exception_does_not_silently_fallback_to_mock():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    with patch("urllib.request.urlopen", side_effect=Exception("API connection failed")):
        response = client.get("/api/transactions?data_inicio_criacao=2026-01-01")
        assert response.status_code == 503
        assert "is temporarily unavailable" in response.json()["detail"] or "failed" in response.json()["detail"].lower()

def test_antifallback_production_mode_imports_mock():
    import pytest
    from fastapi import HTTPException
    from main import validate_dataset_origin
    os.environ["APP_ENV"] = "production"
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    
    with pytest.raises(HTTPException) as exc_info:
        validate_dataset_origin("demo", "synthetic_mock", [])
    assert exc_info.value.status_code == 500
    assert "Critical failure: Live mode in production cannot use mock data" in exc_info.value.detail
    
    os.environ["APP_ENV"] = "development"

@patch("main.get_jwk_client")
def test_jwt_kid_desconhecido_retorna_401(mock_get_jwk_client):
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    try:
        mock_client = MagicMock()
        mock_key = MagicMock()
        mock_key.kid = "some_valid_kid"
        mock_jwk_set = MagicMock()
        mock_jwk_set.keys = [mock_key]
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_client.get_signing_key_from_jwt.side_effect = Exception("Signing key not found")
        mock_get_jwk_client.return_value = mock_client
        
        token = create_mock_jwt()
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 401
        assert "Invalid or expired access token." in res.json()["detail"]
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

def test_jwt_token_aleatorio_retorna_401():
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        test_client = TestClient(app, headers={"Authorization": "Bearer random_string_xyz"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 401
        assert "Invalid or expired access token." in res.json()["detail"]
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

@patch("main.get_jwk_client")
def test_jwt_assinatura_invalida_retorna_401(mock_get_jwk_client):
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        mock_client = MagicMock()
        mock_key = MagicMock()
        mock_key.kid = "mock_kid"
        mock_key.key = "dummy_public_key_which_fails_verification"
        mock_jwk_set = MagicMock()
        mock_jwk_set.keys = [mock_key]
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_client.get_signing_key_from_jwt.return_value = mock_key
        mock_get_jwk_client.return_value = mock_client
        
        token = create_mock_jwt()
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 401
        assert "Invalid or expired access token." in res.json()["detail"]
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

@patch("main.get_jwk_client")
def test_jwks_offline_retorna_503(mock_get_jwk_client):
    from jwt.exceptions import PyJWKClientConnectionError
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        mock_client = MagicMock()
        mock_client.get_jwk_set.side_effect = PyJWKClientConnectionError("Connection timed out")
        mock_get_jwk_client.return_value = mock_client
        
        token = create_mock_jwt()
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 503
        assert res.json()["error_code"] == "supabase_jwks_unavailable"
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

@patch("main.get_jwk_client")
def test_jwks_vazio_retorna_503(mock_get_jwk_client):
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        mock_client = MagicMock()
        mock_jwk_set = MagicMock()
        mock_jwk_set.keys = []
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_get_jwk_client.return_value = mock_client
        
        token = create_mock_jwt()
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 503
        assert res.json()["error_code"] == "supabase_jwks_unavailable"
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

@patch("main.get_jwk_client")
@patch("jwt.decode")
def test_jwt_valido_retorna_200(mock_jwt_decode, mock_get_jwk_client):
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    try:
        mock_client = MagicMock()
        mock_key = MagicMock()
        mock_key.kid = "mock_kid"
        mock_key.key = "dummy_public_key"
        mock_jwk_set = MagicMock()
        mock_jwk_set.keys = [mock_key]
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_client.get_signing_key_from_jwt.return_value = mock_key
        mock_get_jwk_client.return_value = mock_client
        
        mock_jwt_decode.return_value = {
            "email": "corretor@gralhaimoveis.com.br",
            "sub": "mock_user_123",
            "aud": "authenticated",
            "role": "authenticated",
            "iss": "https://mock.supabase.co/auth/v1",
            "exp": time.time() + 3600
        }
        
        token = create_mock_jwt()
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 200
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

def test_jwt_sem_kid_retorna_401():
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        # Create token without kid header
        token = create_mock_jwt(headers={})
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 401
        assert "Invalid or expired access token." in res.json()["detail"]
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)


def test_sequential_pagination_10_pages_and_decimal_precision():
    from unittest.mock import patch, MagicMock
    import urllib.request
    import json

    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"

    # We mock 10 page responses.
    # Pages 1 to 9 have 25 transactions each (total 225).
    # Page 10 has 4 transactions.
    # Total = 229.
    pages = []
    
    # Let's create transactions. To check Decimal precision, let's make their values fractional.
    # Page 1 has values that sum up with complex fractional parts:
    # 25 transactions of 1000000.01 each.
    # Total = 229 transactions.
    tx_index = 1
    for p in range(1, 11):
        num_txs = 25 if p < 10 else 4
        txs_list = []
        for i in range(num_txs):
            txs_list.append({
                "transacao_unique_id_pipeimob": f"tx_seq_{tx_index}",
                "valor_contrato": 1000000.01,
                "total_comissao": 50000.01,
                "codigo_contrato": f"C_{tx_index}",
                "agente_gestor": "JUNIOR SAGAS",
                "midia_origem_compradores": "CORRETOR PORTAIS",
                "etapa_atual": "Escrituração",
                "financiamento": False,
                "data_contrato": "2026-07-02"
            })
            tx_index += 1
        
        pages.append({
            "success": True,
            "data": {
                "transacoes": txs_list,
                "meta": {
                    "pagination": {
                        "total_pages": 10,
                        "current_page": p
                    }
                }
            }
        })

    call_count = 0

    def mock_urlopen(req, *args, **kwargs):
        nonlocal call_count
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        res = MagicMock()
        res.__enter__.return_value = res
        if "/auth" in url:
            res.read.return_value = json.dumps({
                "success": True,
                "data": {
                    "access_token": "mock_auth_token",
                    "expires_in": 3600
                }
            }).encode("utf-8")
            return res
            
        res.read.return_value = json.dumps(pages[call_count]).encode("utf-8")
        call_count += 1
        return res

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        # Trigger /api/dashboard/full with CCV range filter to enable live load
        res = client.get("/api/dashboard/full?data_inicio_ccv=2026-07-01&data_fim_ccv=2026-07-07")
        assert res.status_code == 200
        data = res.json()
        assert data["data_mode"] == "live"
        assert data["pages_fetched"] == 10
        assert data["transaction_count"] == 229
        
        # Checking Decimal precision sum:
        # VGV = 229 * 1000000.01 = 229000002.29
        # commissions = 229 * 50000.01 = 11450002.29
        assert data["summary"]["total_sales"] == 229000002.29
        assert data["summary"]["total_commissions"] == 11450002.29


def test_sequential_pagination_error_aborts_entirely():
    from unittest.mock import patch, MagicMock
    import urllib.request
    import urllib.error
    import json

    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"

    # Page 1 returns valid JSON metadata indicating 2 pages.
    # Page 2 call throws HTTPError
    page1 = {
        "success": True,
        "data": {
            "transacoes": [{"transacao_unique_id_pipeimob": "tx_p1_1", "valor_contrato": 100000.0}],
            "meta": {
                "pagination": {
                    "total_pages": 2,
                    "current_page": 1
                }
            }
        }
    }

    call_count = 0

    def mock_urlopen(req, *args, **kwargs):
        nonlocal call_count
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        res = MagicMock()
        res.__enter__.return_value = res
        if "/auth" in url:
            res.read.return_value = json.dumps({
                "success": True,
                "data": {
                    "access_token": "mock_auth_token",
                    "expires_in": 3600
                }
            }).encode("utf-8")
            return res
            
        if call_count == 0:
            call_count += 1
            res.read.return_value = json.dumps(page1).encode("utf-8")
            return res
        else:
            raise urllib.error.HTTPError("http://example.com", 503, "Unavailable", {}, None)

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        res = client.get("/api/dashboard/full?data_inicio_ccv=2026-07-01")
        # Ensure it failed completely instead of returning page 1 data partially.
        assert res.status_code == 503


def test_sequential_pagination_deduplication():
    from unittest.mock import patch, MagicMock
    import json

    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"

    # Return same tx_id across pages 1 and 2
    page1 = {
        "success": True,
        "data": {
            "transacoes": [{"transacao_unique_id_pipeimob": "dup_1", "valor_contrato": 100.0}],
            "meta": {
                "pagination": {
                    "total_pages": 2,
                    "current_page": 1
                }
            }
        }
    }
    page2 = {
        "success": True,
        "data": {
            "transacoes": [{"transacao_unique_id_pipeimob": "dup_1", "valor_contrato": 100.0}],
            "meta": {
                "pagination": {
                    "total_pages": 2,
                    "current_page": 2
                }
            }
        }
    }

    call_count = 0

    def mock_urlopen(req, *args, **kwargs):
        nonlocal call_count
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        res = MagicMock()
        res.__enter__.return_value = res
        if "/auth" in url:
            res.read.return_value = json.dumps({
                "success": True,
                "data": {
                    "access_token": "mock_auth_token",
                    "expires_in": 3600
                }
            }).encode("utf-8")
            return res
            
        payload = page1 if call_count == 0 else page2
        call_count += 1
        res.read.return_value = json.dumps(payload).encode("utf-8")
        return res

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        res = client.get("/api/dashboard/full?data_inicio_ccv=2026-07-01")
        assert res.status_code == 200
        data = res.json()
        # Ensure count is 1 (fully deduplicated) and total volume is 100.0 (not 200.0)
        assert data["transaction_count"] == 1
        assert data["summary"]["total_sales"] == 100.0


def test_timeline_date_parsing_and_priority():
    from main import extract_transaction_date, parse_date_to_year_month, compute_dashboard_aggregates
    
    # 1. Test priority
    tx1 = {
        "data_assinatura_ccv": "2026-01-01",
        "data_ccv": "2026-02-02",
        "data_contrato": "2026-03-03"
    }
    assert extract_transaction_date(tx1) == "2026-01-01"
    
    tx2 = {
        "data_contrato": "2026-03-03",
        "data_criacao": "2026-04-04"
    }
    assert extract_transaction_date(tx2) == "2026-03-03"
    
    # Nested check
    tx3 = {
        "nested": {
            "data_ccv": "2026-02-02"
        }
    }
    assert extract_transaction_date(tx3) == "2026-02-02"
    
    # 2. Test date formats
    assert parse_date_to_year_month("2026-01-02") == (2026, 1)
    assert parse_date_to_year_month("2026-02-03T12:00:00") == (2026, 2)
    assert parse_date_to_year_month("2026-03-04T12:00:00Z") == (2026, 3)
    assert parse_date_to_year_month("2026-04-05T12:00:00.123Z") == (2026, 4)
    assert parse_date_to_year_month("06/07/2026") == (2026, 7)
    
    # Invalid formats should return None
    assert parse_date_to_year_month("invalid-date") is None
    assert parse_date_to_year_month(None) is None
    
    # 3. Test timeline prepopulation, Decimal precision, empty month, and reconciliation
    filtered_txs = [
        {"data_assinatura_ccv": "2026-01-10", "valor_contrato": 100000.05, "total_comissao": 5000.05},
        {"data_ccv": "2026-02-15T10:00:00", "valor_contrato": 200000.10, "total_comissao": 10000.10},
        {"data_assinatura": "2026-04-20T10:00:00Z", "valor_contrato": 150000.15, "total_comissao": 7500.15},
        {"data_contrato": "30/06/2026", "valor_contrato": 300000.20, "total_comissao": 15000.20},
        # Invalid date transaction (should be unclassified)
        {"data_criacao": "invalid-date", "valor_contrato": 50000.0, "total_comissao": 2500.0},
        # Missing date transaction (should be unclassified)
        {"valor_contrato": 40000.0, "total_comissao": 2000.0}
    ]
    
    res = compute_dashboard_aggregates(
        filtered_txs,
        data_inicio_ccv="2026-01-01",
        data_fim_ccv="2026-06-30"
    )
    
    summary = res["summary"]
    timeline = res["timeline"]
    unclassified = res["unclassified"]
    reconciliation = res["reconciliation"]
    
    assert summary["transaction_count"] == 6
    assert summary["total_sales"] == 840000.50
    assert summary["total_commissions"] == 42000.50
    
    assert len(timeline) == 6
    months = [t["month"] for t in timeline]
    assert months == ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
    
    # Verify that unclassified transactions are NOT assigned to Jan/26 (first month)
    # January should have exactly 1 transaction (the 100000.05 sales transaction)
    assert timeline[0]["transaction_count"] == 1
    assert timeline[0]["total_sales"] == "100000.05"
    assert timeline[0]["total_commissions"] == "5000.05"
    
    # Prepopulated empty months should be 0
    assert timeline[2]["transaction_count"] == 0
    assert timeline[2]["total_sales"] == "0.00"
    assert timeline[2]["total_commissions"] == "0.00"
    
    assert timeline[4]["transaction_count"] == 0
    assert timeline[4]["total_sales"] == "0.00"
    assert timeline[4]["total_commissions"] == "0.00"
    
    # Check unclassified values
    # sales: 50000.0 (invalid) + 40000.0 (missing) = 90000.0
    # commissions: 2500.0 + 2000.0 = 4500.0
    assert unclassified["transaction_count"] == 2
    assert unclassified["total_sales"] == "90000.00"
    assert unclassified["total_commissions"] == "4500.00"
    assert unclassified["missing_date_count"] == 1
    assert unclassified["invalid_date_count"] == 1
    
    # Check reconciliation values
    # rule: timeline totals + unclassified totals = summary totals
    timeline_count_sum = sum(t["transaction_count"] for t in timeline)
    timeline_sales_sum = sum(float(t["total_sales"]) for t in timeline)
    timeline_comm_sum = sum(float(t["total_commissions"]) for t in timeline)
    
    assert timeline_count_sum + unclassified["transaction_count"] == summary["transaction_count"]
    assert round(timeline_sales_sum + float(unclassified["total_sales"]), 2) == summary["total_sales"]
    assert round(timeline_comm_sum + float(unclassified["total_commissions"]), 2) == summary["total_commissions"]
    
    assert reconciliation["is_reconciled"] is True
    assert reconciliation["summary_transaction_count"] == 6
    assert reconciliation["timeline_transaction_count"] == 4
    assert reconciliation["unclassified_transaction_count"] == 2
    
    # 4. Test when all dates are valid
    valid_txs = [
        {"data_assinatura_ccv": "2026-01-10", "valor_contrato": 100000.0, "total_comissao": 5000.0},
        {"data_ccv": "2026-02-15", "valor_contrato": 200000.0, "total_comissao": 10000.0}
    ]
    res_valid = compute_dashboard_aggregates(valid_txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-02-28")
    assert res_valid["unclassified"]["transaction_count"] == 0
    assert res_valid["unclassified"]["total_sales"] == "0.00"
    assert res_valid["unclassified"]["total_commissions"] == "0.00"
    assert res_valid["unclassified"]["missing_date_count"] == 0
    assert res_valid["unclassified"]["invalid_date_count"] == 0
    assert res_valid["reconciliation"]["is_reconciled"] is True
    assert res_valid["reconciliation"]["summary_transaction_count"] == 2
    assert res_valid["reconciliation"]["timeline_transaction_count"] == 2
    assert res_valid["reconciliation"]["unclassified_transaction_count"] == 0
    
    for t in timeline:
        assert "comprador" not in t
        assert "cliente" not in t
        assert "cpf" not in t
        assert "cnpj" not in t
        assert "celular" not in t
        assert "email" not in t


def test_sanitize_transaction_preserves_operational_fields():
    from main import sanitize_transaction
    
    raw_tx = {
        "transacao_unique_id_pipeimob": "123",
        "codigo_contrato": "CON-123",
        "codigo_imovel": "IMO-123",
        "titulo_nome_negocio": "Venda Casa Alpha",
        "data_captacao": "2026-04-10",
        "data_assinatura_ccv": "2026-05-15",
        "data_ccv": "2026-05-15",
        "data_assinatura": "2026-05-15",
        "data_contrato": "2026-05-15",
        "data_criacao": "2026-05-15",
        "created_at": "2026-05-15T10:00:00Z",
        "valor_contrato": 1500000.0,
        "total_comissao": 75000.0,
        "etapa_atual": "Fechamento",
        "midia_origem_compradores": "Portal Imobiliário",
        "agente_gestor": "Eduardo Nascimento",
        "cpf_cliente": "123.456.789-00",
        "email_cliente": "cliente@sensitive.com",
        "celular_cliente": "11988887777",
        "compradores": [
            {"nome": "Comprador Secreto", "cpf": "123.456.789-00", "papel": "Comprador"}
        ],
        "vendedores": [
            {"nome": "Vendedor Privado", "cnpj": "12.345.678/0001-99", "papel": "Vendedor"}
        ],
        "clientes": [
            {"nome": "Cliente Privado", "papel": "Comprador"}
        ]
    }
    
    sanitized = sanitize_transaction(raw_tx)
    
    assert sanitized["transacao_unique_id_pipeimob"] == "123"
    assert sanitized["codigo_contrato"] == "CON-123"
    assert sanitized["codigo_imovel"] == "IMO-123"
    assert sanitized["titulo_nome_negocio"] == "Venda Casa Alpha"
    assert sanitized["data_captacao"] == "2026-04-10"
    assert sanitized["data_assinatura_ccv"] == "2026-05-15"
    assert sanitized["data_ccv"] == "2026-05-15"
    assert sanitized["data_assinatura"] == "2026-05-15"
    assert sanitized["data_contrato"] == "2026-05-15"
    assert sanitized["data_criacao"] == "2026-05-15"
    assert sanitized["created_at"] == "2026-05-15T10:00:00Z"
    assert sanitized["valor_contrato"] == 1500000.0
    assert sanitized["total_comissao"] == 75000.0
    assert sanitized["etapa_atual"] == "Fechamento"
    assert sanitized["midia_origem_compradores"] == "Portal Imobiliário"
    assert sanitized["agente_gestor"] == "Eduardo Nascimento"
    
    assert sanitized["compradores"] == 1
    assert sanitized["vendedores"] == 1
    
    assert "cpf_cliente" not in sanitized
    assert "email_cliente" not in sanitized
    assert "celular_cliente" not in sanitized
    assert "clientes" not in sanitized


def test_dashboard_full_contract_schema_and_debug_metrics_behavior():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    
    # 1. Test with ENABLE_SAFE_DEBUG_METRICS=false (default/unset)
    os.environ["ENABLE_SAFE_DEBUG_METRICS"] = "false"
    response = client.get("/api/dashboard/full?data_inicio_ccv=2026-01-01&data_fim_ccv=2026-06-30")
    assert response.status_code == 200
    data = response.json()
    
    assert "data_mode" in data
    assert "source" in data
    assert "period" in data
    assert "pages_fetched" in data
    assert "transaction_count" in data
    assert "summary" in data
    assert "timeline" in data
    assert "origins" in data
    assert "stages" in data
    assert "managers" in data
    assert "payments" in data
    assert "commissions" in data
    
    assert data.get("schema_version") == "1.0"
    assert "generated_at" in data
    assert "filters_applied" in data
    assert data["filters_applied"].get("data_inicio_ccv") == "2026-01-01"
    assert data["filters_applied"].get("data_fim_ccv") == "2026-06-30"
    
    assert data.get("debug_metrics") is None
    
    # 2. Test with ENABLE_SAFE_DEBUG_METRICS=true
    os.environ["ENABLE_SAFE_DEBUG_METRICS"] = "true"
    response_debug = client.get("/api/dashboard/full?data_inicio_ccv=2026-01-01&data_fim_ccv=2026-06-30")
    assert response_debug.status_code == 200
    data_debug = response_debug.json()
    assert data_debug.get("debug_metrics") is not None
    assert "priority_keys_presence" in data_debug["debug_metrics"]
    
    resp_str = json.dumps(data_debug)
    for pii_term in ["cpf", "cnpj", "celular", "email", "documentos", "link_acesso"]:
        assert pii_term not in resp_str.lower() or "count" in pii_term or "unclassified" in pii_term or "reconciliation" in pii_term or "debug_metrics" in pii_term


def test_timeline_equal_summary_when_all_dates_valid():
    from main import compute_dashboard_aggregates
    
    valid_txs = [
        {"data_assinatura_ccv": "2026-01-10", "valor_contrato": 100000.0, "total_comissao": 5000.0},
        {"data_ccv": "2026-02-15", "valor_contrato": 200000.0, "total_comissao": 10000.0},
        {"data_contrato": "2026-03-20", "valor_contrato": 300000.0, "total_comissao": 15000.0}
    ]
    
    res = compute_dashboard_aggregates(valid_txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-03-31")
    
    summary = res["summary"]
    timeline = res["timeline"]
    unclassified = res["unclassified"]
    
    assert unclassified["transaction_count"] == 0
    assert unclassified["out_of_range_count"] == 0
    assert unclassified["missing_date_count"] == 0
    assert unclassified["invalid_date_count"] == 0
    
    timeline_count_sum = sum(t["transaction_count"] for t in timeline)
    timeline_sales_sum = sum(float(t["total_sales"]) for t in timeline)
    timeline_comm_sum = sum(float(t["total_commissions"]) for t in timeline)
    
    assert timeline_count_sum == summary["transaction_count"]
    assert abs(timeline_sales_sum - summary["total_sales"]) < 0.01
    assert abs(timeline_comm_sum - summary["total_commissions"]) < 0.01


def test_no_artificial_boundary_month_assignment():
    from main import compute_dashboard_aggregates
    
    txs = [
        {"data_assinatura_ccv": "2026-04-10", "valor_contrato": 500000.0, "total_comissao": 25000.0},
        {"data_assinatura_ccv": "2026-01-15", "valor_contrato": 100000.0, "total_comissao": 5000.0},
    ]
    
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-03-31")
    
    timeline = res["timeline"]
    unclassified = res["unclassified"]
    
    assert timeline[0]["transaction_count"] == 1
    assert timeline[2]["transaction_count"] == 0
    
    assert unclassified["transaction_count"] == 1
    assert unclassified["out_of_range_count"] == 1


from unittest.mock import patch, MagicMock

def test_live_pagination_229_records():
    from main import fetch_all_pipeimob_transactions
    
    responses = []
    
    for p in range(1, 10):
        txs = []
        for i in range(25):
            txs.append({
                "transacao_unique_id_pipeimob": f"tx_pag_{p}_{i}",
                "valor_contrato": 1400000.0,
                "total_comissao": 74786.45554585,
                "data_assinatura_ccv": "2026-03-15"
            })
        responses.append({
            "success": True,
            "data": {
                "transacoes": txs
            },
            "meta": {
                "pagination": {
                    "current_page": p,
                    "total_pages": 10,
                    "total_records": 229
                }
            }
        })
        
    txs_10 = []
    for i in range(4):
        txs_10.append({
            "transacao_unique_id_pipeimob": f"tx_pag_10_{i}",
            "valor_contrato": 1608779.4725,
            "total_comissao": 74786.45554585,
            "data_assinatura_ccv": "2026-03-15"
        })
    responses.append({
        "success": True,
        "data": {
            "transacoes": txs_10
        },
        "meta": {
            "pagination": {
                "current_page": 10,
                "total_pages": 10,
                "total_records": 229
            }
        }
    })
    
    with patch("urllib.request.urlopen") as mock_urlopen, \
         patch("main.get_auth_token", return_value="mock_access_token"):
         
        mock_res_objects = []
        for r in responses:
            mock_res = MagicMock()
            mock_res.__enter__.return_value = mock_res
            mock_res.read.return_value = json.dumps(r).encode("utf-8")
            mock_res.getcode.return_value = 200
            mock_res_objects.append(mock_res)
            
        mock_urlopen.side_effect = mock_res_objects
        
        txs, pages = fetch_all_pipeimob_transactions(
            api_key="mock_key",
            api_secret="mock_secret",
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30"
        )
        
        assert len(txs) == 229
        assert pages == 10
        
        from main import compute_dashboard_aggregates
        aggregates = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
        
        summary = aggregates["summary"]
        assert summary["transaction_count"] == 229
        assert float(summary["total_sales"]) == 321435117.89
        assert float(summary["total_commissions"]) == 17126098.32


def test_vgc_commission_composition_canonical():
    from main import compute_dashboard_aggregates
    
    txs = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"nome": "Imobiliária Gralha", "tipo": "Gralha Imobiliária", "valor": 3000.0, "comissionado_imobiliaria": True, "comissionado_valor": 3000.0},
                {"nome": "Gralha Filial", "tipo": "Empresa", "valor": 2000.0, "comissionado_imobiliaria": True, "comissionado_valor": 2000.0},
                {"nome": "Imobiliária Externa", "tipo": "Imobiliária", "valor": 1000.0, "comissionado_imobiliaria": False, "comissionado_valor": 1000.0},
                {"nome": "Corretor X", "tipo": "Corretor", "valor": 4000.0, "comissionado_imobiliaria": False, "comissionado_valor": 4000.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    assert financials["vgc_total"] == "10000.00"
    assert financials["composition"]["gralha"] == "5000.00"
    assert financials["composition"]["demais_participantes"] == "5000.00"
    assert financials["composition"]["reconciled"] is True
    
    txs_empty = [
        {
            "total_comissao": 0.0,
            "comissionados": None,
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_empty = compute_dashboard_aggregates(txs_empty, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    assert res_empty["commission_financials"]["vgc_total"] == "0.00"
    assert res_empty["commission_financials"]["composition"]["gralha"] == "0.00"
    assert res_empty["commission_financials"]["composition"]["demais_participantes"] == "0.00"
    
    txs_invalid_val = [
        {
            "total_comissao": 5000.0,
            "comissionados": [
                {"nome": "Gralha", "tipo": "Empresa", "valor": "invalid_value", "comissionado_imobiliaria": True, "comissionado_valor": "invalid_value"}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_invalid = compute_dashboard_aggregates(txs_invalid_val, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    assert res_invalid["commission_financials"]["composition"]["gralha"] == "0.00"
    assert res_invalid["commission_financials"]["composition"]["demais_participantes"] == "0.00"
    assert res_invalid["commission_financials"]["vgc_composition"]["unclassified"]["amount"] == "5000.00"


def test_vgc_reconciliation_integrity():
    from main import compute_dashboard_aggregates
    
    txs_inconsistent = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"nome": "Gralha", "tipo": "Empresa", "valor": 12000.0, "comissionado_imobiliaria": True, "comissionado_valor": 12000.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs_inconsistent, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    assert financials["composition"]["reconciled"] is False
    assert float(financials["composition"]["demais_participantes"]) == 0.0
    assert float(financials["vgc_composition"]["unclassified"]["amount"]) == 10000.0


def test_vgc_receipt_date_status_only():
    from main import compute_dashboard_aggregates
    
    txs = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"nome": "Gralha", "tipo": "Empresa", "valor": 4000.0, "comissionado_imobiliaria": True, "comissionado_valor": 4000.0},
                {"nome": "Outro", "tipo": "Corretor", "valor": 6000.0, "comissionado_valor": 6000.0}
            ],
            "data_assinatura_ccv": "2026-03-15",
            "data_recebimento_comissao": "2026-04-10" # Past date relative to now (which is 2026-07-17)
        },
        {
            "total_comissao": 5000.0,
            "comissionados": [
                {"nome": "Gralha", "tipo": "Empresa", "valor": 2000.0, "comissionado_imobiliaria": True, "comissionado_valor": 2000.0},
                {"nome": "Outro", "tipo": "Corretor", "valor": 3000.0, "comissionado_valor": 3000.0}
            ],
            "data_assinatura_ccv": "2026-03-15",
            "data_recebimento_comissao": "" # Empty date -> pending_no_date
        },
        {
            "total_comissao": 3000.0,
            "comissionados": [
                {"nome": "Gralha", "tipo": "Empresa", "valor": 1000.0, "comissionado_imobiliaria": True, "comissionado_valor": 1000.0},
                {"nome": "Outro", "tipo": "Corretor", "valor": 2000.0, "comissionado_valor": 2000.0}
            ],
            "data_assinatura_ccv": "2026-03-15",
            "data_recebimento_comissao": "not-a-valid-date" # Invalid date -> unknown_invalid_date
        }
    ]
    
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    assert financials["calculation_method"] == "registered_receipt_date_v1"
    assert financials["allocation_method"] == "status_only"
    
    assert financials["received"]["total"] == "10000.00"
    assert financials["received"]["gralha"] == "4000.00"
    assert financials["received"]["demais_participantes"] == "6000.00"
    assert financials["received"]["transaction_count"] == 1
    
    assert financials["pending"]["total"] == "5000.00"
    assert financials["pending"]["gralha"] == "2000.00"
    assert financials["pending"]["demais_participantes"] == "3000.00"
    assert financials["pending"]["transaction_count"] == 1
    assert financials["pending"]["without_date_count"] == 1
    assert financials["pending"]["future_date_count"] == 0
    
    assert financials["unknown"]["total"] == "3000.00"
    assert financials["unknown"]["gralha"] == "1000.00"
    assert financials["unknown"]["demais_participantes"] == "2000.00"
    assert financials["unknown"]["transaction_count"] == 1
    assert financials["unknown"]["invalid_date_count"] == 1


def test_vgc_receipt_proportional_allocation():
    from main import compute_dashboard_aggregates
    
    txs = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"nome": "Gralha", "tipo": "Empresa", "valor": 6000.0, "comissionado_imobiliaria": True, "comissionado_valor": 6000.0},
                {"nome": "Outro", "tipo": "Corretor", "valor": 4000.0, "comissionado_valor": 4000.0}
            ],
            "data_assinatura_ccv": "2026-03-15",
            "valor_recebido": 3000.0 # Under V1, we only classify by date. No date -> pending_no_date
        }
    ]
    
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    assert financials["calculation_method"] == "registered_receipt_date_v1"
    assert financials["allocation_method"] == "status_only"
    
    assert financials["received"]["total"] == "0.00"
    assert financials["pending"]["total"] == "10000.00"
    assert financials["pending"]["without_date_count"] == 1


def test_vgc_v1_classification_comprehensive():
    from main import compute_dashboard_aggregates
    from zoneinfo import ZoneInfo
    from datetime import datetime, timedelta
    
    sp_tz = ZoneInfo("America/Sao_Paulo")
    now_sp = datetime.now(sp_tz)
    today_str = now_sp.strftime("%Y-%m-%d")
    yesterday_str = (now_sp - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_str = (now_sp + timedelta(days=1)).strftime("%Y-%m-%d")
    
    txs = [
        # Today's date -> received
        {
            "total_comissao": 1000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.0}],
            "data_recebimento_comissao": today_str
        },
        # Yesterday's date -> received
        {
            "total_comissao": 2000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 2000.0}],
            "data_recebimento_comissao": yesterday_str
        },
        # Tomorrow's date -> unknown (future)
        {
            "total_comissao": 3000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 3000.0}],
            "data_recebimento_comissao": tomorrow_str
        },
        # Missing date -> pending (no date)
        {
            "total_comissao": 4000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 4000.0}],
            "data_recebimento_comissao": None
        },
        # Empty string date -> pending (no date)
        {
            "total_comissao": 5000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 5000.0}],
            "data_recebimento_comissao": "   "
        },
        # Invalid date format -> unknown
        {
            "total_comissao": 6000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 6000.0}],
            "data_recebimento_comissao": "invalid-format"
        },
        # DD/MM/YYYY format -> received
        {
            "total_comissao": 7000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 7000.0}],
            "data_recebimento_comissao": "10/05/2026"
        },
        # ISO 8601 datetime -> received
        {
            "total_comissao": 8000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 8000.0}],
            "data_recebimento_comissao": "2026-05-20T15:30:00Z"
        },
        # Priority: data_pagamento_comissao_prevista (past/yesterday) wins over data_recebimento_comissao (future/tomorrow) -> received
        {
            "total_comissao": 9000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 9000.0}],
            "data_recebimento_comissao": tomorrow_str,
            "data_pagamento_comissao_prevista": yesterday_str
        },
        # Fallback to data_pagamento_comissao_prevista if data_recebimento_comissao is missing -> received
        {
            "total_comissao": 10000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 10000.0}],
            "data_recebimento_comissao": None,
            "data_pagamento_comissao_prevista": yesterday_str
        }
    ]
    
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    
    assert financials["as_of_date"] == today_str
    assert financials["timezone"] == "America/Sao_Paulo"
    assert financials["semantic_validation"] == "provisional_v1"
    
    assert financials["received"]["transaction_count"] == 6
    assert financials["pending"]["future_date_count"] == 0
    assert financials["pending"]["without_date_count"] == 2
    assert financials["pending"]["transaction_count"] == 2
    assert financials["unknown"]["transaction_count"] == 2
    assert financials["unknown"]["invalid_date_count"] == 2
    
    assert financials["receipt_date_sources"]["data_recebimento_comissao"] == 6
    assert financials["receipt_date_sources"]["data_pagamento_comissao_prevista"] == 2
    assert financials["receipt_date_sources"]["missing"] == 2
    
    assert financials["composition"]["reconciled"] is True


def test_sales_cycle_comprehensive():
    from main import compute_dashboard_aggregates
    
    txs = [
        # captação e assinatura no mesmo dia (0 dias) -> bucket 0_30_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-01-10"
        },
        # diferença de 1 dia (1 dia) -> bucket 0_30_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-01-11"
        },
        # exatamente 30 dias (30 dias) -> bucket 0_30_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-02-09"
        },
        # exatamente 31 dias (31 dias) -> bucket 31_60_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-02-10"
        },
        # exatamente 60 dias (60 dias) -> bucket 31_60_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-03-11"
        },
        # exatamente 61 dias (61 dias) -> bucket 61_90_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-03-12"
        },
        # exatamente 90 dias (90 dias) -> bucket 61_90_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-04-10"
        },
        # exatamente 91 dias (91 dias) -> bucket 91_180_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-04-11"
        },
        # exatamente 180 dias (180 dias) -> bucket 91_180_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-07-09"
        },
        # exatamente 181 dias (181 dias) -> bucket 181_365_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-07-10"
        },
        # exatamente 365 dias (365 dias) -> bucket 181_365_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2027-01-10"
        },
        # exatamente 366 dias (366 dias) -> bucket over_365_days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2027-01-11"
        },
        # captação ausente -> excluded (missing_capture_date_count)
        {
            "data_captacao": None,
            "data_assinatura_ccv": "2026-01-10"
        },
        # assinatura ausente -> excluded (missing_signature_date_count)
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": ""
        },
        # data inválida -> excluded (invalid_date_count)
        {
            "data_captacao": "not-a-date",
            "data_assinatura_ccv": "2026-01-10"
        },
        # captação posterior à assinatura -> excluded (negative_duration_count)
        {
            "data_captacao": "2026-01-15",
            "data_assinatura_ccv": "2026-01-10"
        },
        # formato DD/MM/YYYY
        {
            "data_captacao": "10/01/2026",
            "data_assinatura_ccv": "15/01/2026"  # 5 dias -> bucket 0_30_days
        },
        # formato ISO 8601
        {
            "data_captacao": "2026-01-10T12:00:00Z",
            "data_assinatura_ccv": "2026-01-20T15:30:00Z"  # 10 dias -> bucket 0_30_days
        }
    ]
    
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    sc = res["sales_cycle"]
    
    assert sc["period_basis"] == "ccv"
    assert sc["start_field"] == "data_captacao"
    assert sc["end_field"] == "data_assinatura_ccv"
    assert sc["calculation_unit"] == "days"
    
    # Excluded counts assertions
    assert sc["excluded"]["missing_capture_date_count"] == 1
    assert sc["excluded"]["missing_signature_date_count"] == 1
    assert sc["excluded"]["invalid_date_count"] == 1
    assert sc["excluded"]["negative_duration_count"] == 1
    
    # 18 total transactions, 4 excluded -> 14 valid
    assert sc["transaction_count"] == 18
    assert sc["valid_transaction_count"] == 14
    
    # Durations are:
    # 0, 1, 30, 31, 60, 61, 90, 91, 180, 181, 365, 366, 5, 10
    # Sorted: [0, 1, 5, 10, 30, 31, 60, 61, 90, 91, 180, 181, 365, 366]
    # Sum: 0+1+5+10+30+31+60+61+90+91+180+181+365+366 = 1471
    # Average: 1471 / 14 = 105.071... -> 105.1
    assert sc["average_days"] == 105.1
    
    # Mediana (even count N=14): idx = 0.5 * 13 = 6.5 -> low=6 (60), high=7 (61) -> 60.5
    assert sc["median_days"] == 60.5
    
    # p25: idx = 0.25 * 13 = 3.25 -> low=3 (10), high=4 (30) -> 10 + 0.25 * 20 = 15.0
    assert sc["p25_days"] == 15.0
    
    # p75: idx = 0.75 * 13 = 9.75 -> low=9 (91), high=10 (180) -> 91 + 0.75 * 89 = 157.75 -> 157.8
    assert sc["p75_days"] == 157.8
    
    # p90: idx = 0.90 * 13 = 11.7 -> low=11 (181), high=12 (365) -> 181 + 0.7 * 184 = 309.8
    assert sc["p90_days"] == 309.8
    
    assert sc["minimum_days"] == 0
    assert sc["maximum_days"] == 366
    
    # Buckets count:
    # 0_30_days: [0, 1, 5, 10, 30] -> 5
    # 31_60_days: [31, 60] -> 2
    # 61_90_days: [61, 90] -> 2
    # 91_180_days: [91, 180] -> 2
    # 181_365_days: [181, 365] -> 2
    # over_365_days: [366] -> 1
    # Check bucket totals
    assert sc["buckets"][0]["count"] == 5
    assert sc["buckets"][1]["count"] == 2
    assert sc["buckets"][2]["count"] == 2
    assert sc["buckets"][3]["count"] == 2
    assert sc["buckets"][4]["count"] == 2
    assert sc["buckets"][5]["count"] == 1
    
    # valid sum bucket count check
    assert sum(b["count"] for b in sc["buckets"]) == 14
    
    # within counts
    # within 30: 5
    # within 60: 7
    # within 90: 9
    assert sc["within_30_days_count"] == 5
    assert sc["within_60_days_count"] == 7
    assert sc["within_90_days_count"] == 9
    assert sc["within_90_days_ratio"] == round(9 / 14, 4)
    
    # within 90 counts matches first three buckets sum check
    assert sc["within_90_days_count"] == (sc["buckets"][0]["count"] + sc["buckets"][1]["count"] + sc["buckets"][2]["count"])
    
    # Quantity reconciliations check
    assert (
        sc["valid_transaction_count"] +
        sc["excluded"]["missing_signature_date_count"] +
        sc["excluded"]["missing_capture_date_count"] +
        sc["excluded"]["invalid_date_count"] +
        sc["excluded"]["negative_duration_count"]
    ) == sc["transaction_count"]
    
    # Check no PII leakage in payload
    keys_allowed = {
        "period_basis", "start_field", "end_field", "calculation_unit",
        "transaction_count", "valid_transaction_count", "excluded",
        "average_days", "median_days", "p25_days", "p75_days", "p90_days",
        "minimum_days", "maximum_days", "within_30_days_count", "within_60_days_count",
        "within_90_days_count", "within_90_days_ratio", "buckets", "timeline",
        "fastest_sale", "longest_sale"
    }
    assert set(sc.keys()) == keys_allowed


@patch("main.load_transactions_dataset", new_callable=AsyncMock)
def test_dashboard_full_endpoint_sales_cycle(mock_load):
    from unittest.mock import patch
    from main import app
    
    # Set mock dataset
    mock_load.return_value = (
        "demo",
        "synthetic_mock",
        [
            {
                "data_captacao": "2026-01-10",
                "data_assinatura_ccv": "2026-01-20"  # 10 days
            },
            {
                "data_captacao": "2026-01-10",
                "data_assinatura_ccv": "2026-01-30"  # 20 days
            }
        ],
        1,
        "miss"
    )
    
    # Authenticate using mock JWT token
    try:
        test_client = TestClient(app, headers={"Authorization": f"Bearer {mock_token}"})
        res = test_client.get("/api/dashboard/full?data_inicio_ccv=2026-01-01&data_fim_ccv=2026-06-30")
        assert res.status_code == 200
        data = res.json()
        assert "sales_cycle" in data
        sc = data["sales_cycle"]
        assert sc is not None
        assert sc["transaction_count"] == 2
        assert sc["valid_transaction_count"] == 2
        assert sc["median_days"] == 15.0  # Median of [10, 20] is 15.0
        assert len(sc["buckets"]) == 6
        assert len(sc["timeline"]) == 6
        assert "fastest_sale" in sc
        assert "longest_sale" in sc
        assert sc["fastest_sale"]["days"] == 10
        assert sc["longest_sale"]["days"] == 20
    finally:
        pass


def test_sales_cycle_extremes_comprehensive():
    from main import compute_dashboard_aggregates
    
    # Test case 1: general check of fastest and longest sale, days=0, PII sanitization
    txs = [
        # Fastest sale: 0 days
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-01-10",
            "codigo_imovel": "IMO-FASTEST",
            "titulo_nome_negocio": "Negócio Alfa",
            "transacao_unique_id_pipeimob": "uid-fast"
        },
        # Intermediate sale
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-01-20",
            "codigo_imovel": "IMO-INTER",
            "titulo_nome_negocio": "Negócio Inter",
            "transacao_unique_id_pipeimob": "uid-inter"
        },
        # Longest sale: 100 days, has sensitive info (email and CNPJ) to verify sanitization
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-04-20",
            "codigo_imovel": "IMO-LONGEST",
            "titulo_nome_negocio": "Contato: imob@gralha.com.br CNPJ 12.345.678/0001-99",
            "transacao_unique_id_pipeimob": "uid-long"
        },
        # Excluded sale (invalid date) - must not be selected as extreme
        {
            "data_captacao": "invalid-date",
            "data_assinatura_ccv": "2026-01-10",
            "codigo_imovel": "IMO-EXCLUDED",
            "titulo_nome_negocio": "Negócio Excluded",
            "transacao_unique_id_pipeimob": "uid-ex"
        }
    ]
    
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    sc = res["sales_cycle"]
    
    assert sc["valid_transaction_count"] == 3
    assert sc["minimum_days"] == 0
    assert sc["maximum_days"] == 100
    
    # Verify fastest sale properties
    assert sc["fastest_sale"] is not None
    assert sc["fastest_sale"]["days"] == 0
    assert sc["fastest_sale"]["property_code"] == "IMO-FASTEST"
    assert sc["fastest_sale"]["deal_title"] == "Negócio Alfa"
    assert sc["fastest_sale"]["days"] == sc["minimum_days"]
    assert set(sc["fastest_sale"].keys()) == {"days", "property_code", "deal_title"}
    
    # Verify longest sale properties & sanitization of email/CNPJ
    assert sc["longest_sale"] is not None
    assert sc["longest_sale"]["days"] == 100
    assert sc["longest_sale"]["property_code"] == "IMO-LONGEST"
    assert sc["longest_sale"]["deal_title"] is None  # Sanitized to None because of email/CNPJ!
    assert sc["longest_sale"]["days"] == sc["maximum_days"]
    assert set(sc["longest_sale"].keys()) == {"days", "property_code", "deal_title"}

    # Test case 2: tie-breaker on duration
    txs_tie = [
        # Tie on 5 days duration
        {
            "data_captacao": "2026-01-15",
            "data_assinatura_ccv": "2026-01-20",  # oldest signature date
            "codigo_imovel": "IMO-TIE-C",
            "titulo_nome_negocio": "Negócio C",
            "transacao_unique_id_pipeimob": "uid-c"
        },
        {
            "data_captacao": "2026-02-15",
            "data_assinatura_ccv": "2026-02-20",  # newer signature date
            "codigo_imovel": "IMO-TIE-A",
            "titulo_nome_negocio": "Negócio A",
            "transacao_unique_id_pipeimob": "uid-a"
        },
        {
            "data_captacao": "2026-01-15",
            "data_assinatura_ccv": "2026-01-20",  # oldest signature date, but code is null (comes after prefilled codes)
            "codigo_imovel": None,
            "titulo_nome_negocio": "Negócio NullCode",
            "transacao_unique_id_pipeimob": "uid-null-code"
        },
        {
            "data_captacao": "2026-01-15",
            "data_assinatura_ccv": "2026-01-20",  # oldest signature date, same code "IMO-TIE-C", but UID is null
            "codigo_imovel": "IMO-TIE-C",
            "titulo_nome_negocio": "Negócio NullUid",
            "transacao_unique_id_pipeimob": None
        }
    ]
    
    res_tie = compute_dashboard_aggregates(txs_tie, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    sc_tie = res_tie["sales_cycle"]
    
    # Fastest tie-breaker:
    assert sc_tie["fastest_sale"]["days"] == 5
    assert sc_tie["fastest_sale"]["property_code"] == "IMO-TIE-C"
    assert sc_tie["fastest_sale"]["deal_title"] == "Negócio C"
    
    # Test case 3: empty strings in code/title
    txs_empty = [
        {
            "data_captacao": "2026-01-10",
            "data_assinatura_ccv": "2026-01-15",
            "codigo_imovel": "   ",
            "titulo_nome_negocio": "   ",
            "transacao_unique_id_pipeimob": "uid-empty"
        }
    ]
    res_empty = compute_dashboard_aggregates(txs_empty, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    sc_empty = res_empty["sales_cycle"]
    assert sc_empty["fastest_sale"]["property_code"] is None
    assert sc_empty["fastest_sale"]["deal_title"] is None
    
    # Test case 4: no valid transactions
    res_none = compute_dashboard_aggregates([], data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    assert res_none["sales_cycle"]["fastest_sale"] is None
    assert res_none["sales_cycle"]["longest_sale"] is None

def test_parse_official_team_groups_scenarios(monkeypatch):
    from main import parse_official_team_groups
    # 1. missing
    monkeypatch.delenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", raising=False)
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "missing"
    assert not configured
    
    # 2. invalid JSON
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", "not-a-json")
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "invalid"
    
    # 3. root not a list
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", '{"id": "1"}')
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "invalid"
    
    # 4. incomplete - empty list
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", "[]")
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "incomplete"
    
    # 5. incomplete - missing name
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", '[{"id": "1", "type": "team"}]')
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "incomplete"
    
    # 6. incomplete - invalid type
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", '[{"id": "1", "name": "Equipe A", "type": "invalid_type"}]')
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "incomplete"
    
    # 7. incomplete - duplicate IDs
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", '[{"id": "1", "name": "Equipe A", "type": "team"}, {"id": "1", "name": "Equipe B", "type": "team"}]')
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "incomplete"
    
    # 8. incomplete - duplicate team names
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", '[{"id": "1", "name": "Equipe A", "type": "team"}, {"id": "2", "name": "equipe a", "type": "team"}]')
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "incomplete"
    
    # 9. incomplete - no team type
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", '[{"id": "1", "name": "Filial A", "type": "branch"}]')
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "incomplete"
    
    # 10. configured
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", '[{"id": "1", "name": "Equipe A", "type": "team"}, {"id": "2", "name": "Filial A", "type": "branch"}]')
    status, configured, mapping, teams = parse_official_team_groups()
    assert status == "configured"
    assert configured
    assert teams == ["Equipe A"]
    assert mapping["1"] == {"name": "Equipe A", "type": "team"}

def test_data_quality_aggregation_scenarios(monkeypatch):
    from main import compute_dashboard_aggregates
    
    config_json = json.dumps([
        {"id": "group_team_1", "name": "Equipe Alpha", "type": "team"},
        {"id": "group_team_2", "name": "Equipe Beta", "type": "team"},
        {"id": "group_branch_1", "name": "Filial Norte", "type": "branch"},
        {"id": "group_other_1", "name": "Outro Grupo", "type": "other"}
    ])
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", config_json)
    
    # 1. Configured - groups empty -> missing_team_assignment (high)
    txs = [{
        "transacao_unique_id_pipeimob": "tx1",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": []
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 1
    assert dq["summary"]["compliant_agents_count"] == 0
    assert dq["summary"]["affected_transactions_count"] == 1
    assert dq["summary"]["compliant_transactions_count"] == 0
    assert dq["summary"]["transaction_compliance_ratio"] == 0.0
    
    # 2. Configured - branch group only -> missing_team_assignment (high)
    txs = [{
        "transacao_unique_id_pipeimob": "tx2",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_branch_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 1
    
    # 3. Configured - other group only -> missing_team_assignment (high)
    txs = [{
        "transacao_unique_id_pipeimob": "tx3",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_other_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 1
    
    # 4. Configured - branch + unknown -> affected
    txs = [{
        "transacao_unique_id_pipeimob": "tx4",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_branch_1", "group_unknown_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 1
    assert dq["summary"]["review_only_agents_count"] == 0
    
    # 5. Configured - unknown ID only -> review_only
    txs = [{
        "transacao_unique_id_pipeimob": "tx5",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_unknown_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 0
    assert dq["summary"]["review_only_agents_count"] == 1

    # 5b. Configured - team + unknown -> compliant (since there is a valid team)
    txs = [{
        "transacao_unique_id_pipeimob": "tx5b",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_team_1", "group_unknown_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 0
    assert dq["summary"]["review_only_agents_count"] == 0
    assert dq["summary"]["compliant_agents_count"] == 1
    
    # 6. Configured - valid team -> compliant
    txs = [{
        "transacao_unique_id_pipeimob": "tx6",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["compliant_agents_count"] == 1
    
    # 7. Unassigned manager transaction
    txs = [{
        "transacao_unique_id_pipeimob": "tx7",
        "agente_gestor": None,
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 0
    assert dq["summary"]["distinct_agents_count"] == 0
    assert dq["summary"]["affected_transactions_count"] == 1
    assert dq["summary"]["unassigned_manager_transactions_count"] == 1
    
    # 8. Same agent with team and empty -> affected
    txs = [
        {
            "transacao_unique_id_pipeimob": "tx8a",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        },
        {
            "transacao_unique_id_pipeimob": "tx8b",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": []
        }
    ]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 1
    assert dq["summary"]["compliant_agents_count"] == 0
    
    # 9. Same agent in different branches (composite key grouping)
    txs = [
        {
            "transacao_unique_id_pipeimob": "tx9a",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial Alpha",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        },
        {
            "transacao_unique_id_pipeimob": "tx9b",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial Beta",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        }
    ]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["distinct_agents_count"] == 2
    assert dq["summary"]["compliant_agents_count"] == 2
    
    # 10. Agent with error and review -> affected
    txs = [
        {
            "transacao_unique_id_pipeimob": "tx10a",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": []
        },
        {
            "transacao_unique_id_pipeimob": "tx10b",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1", "group_unknown_1"]
        }
    ]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 1
    assert dq["summary"]["review_only_agents_count"] == 0
    
    # 11. Inconsistent team assignment
    txs = [
        {
            "transacao_unique_id_pipeimob": "tx11a",
            "agente_gestor": "Corretor Theta",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        },
        {
            "transacao_unique_id_pipeimob": "tx11b",
            "agente_gestor": "Corretor Theta",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_2"]
        }
    ]
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    assert dq["summary"]["affected_agents_count"] == 0
    assert dq["summary"]["review_only_agents_count"] == 1
    
    # 12. Overall statuses
    txs = [{
        "transacao_unique_id_pipeimob": "tx12a",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    assert res["data_quality"]["summary"]["status"] == "ok"
    
    txs = [
        {
            "transacao_unique_id_pipeimob": "tx12b_1",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": []
        },
        {
            "transacao_unique_id_pipeimob": "tx12b_2",
            "agente_gestor": "Corretor B",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        }
    ]
    res = compute_dashboard_aggregates(txs)
    assert res["data_quality"]["summary"]["status"] == "critical"
    
    txs = [
        {
            "transacao_unique_id_pipeimob": "tx12c_1",
            "agente_gestor": "Corretor A",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": []
        },
        {
            "transacao_unique_id_pipeimob": "tx12c_2",
            "agente_gestor": "Corretor B",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        },
        {
            "transacao_unique_id_pipeimob": "tx12c_3",
            "agente_gestor": "Corretor C",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        },
        {
            "transacao_unique_id_pipeimob": "tx12c_4",
            "agente_gestor": "Corretor D",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
        }
    ]
    res = compute_dashboard_aggregates(txs)
    assert res["data_quality"]["summary"]["status"] == "attention"
    
    # 13. distinct_agents_count = 0 with unassigned manager transactions
    txs = [{
        "transacao_unique_id_pipeimob": "tx13",
        "agente_gestor": None,
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["group_team_1"]
    }]
    res = compute_dashboard_aggregates(txs)
    assert res["data_quality"]["summary"]["status"] == "attention"
    
    # 14. zero transactions
    res = compute_dashboard_aggregates([])
    assert res["data_quality"]["summary"]["status"] == "ok"
    assert res["data_quality"]["summary"]["distinct_agents_count"] == 0
    assert res["data_quality"]["summary"]["agent_compliance_ratio"] == 0.0
    assert res["data_quality"]["summary"]["transaction_compliance_ratio"] == 0.0

    # 15. Missing/Invalid/Incomplete configuration scenarios
    monkeypatch.delenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", raising=False)
    txs = [{
        "transacao_unique_id_pipeimob": "tx15a",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": []
    }]
    res = compute_dashboard_aggregates(txs)
    assert res["data_quality"]["summary"]["affected_agents_count"] == 1
    assert res["data_quality"]["summary"]["review_only_agents_count"] == 0
    
    txs = [{
        "transacao_unique_id_pipeimob": "tx15b",
        "agente_gestor": "Corretor A",
        "agente_gestor_grupo_filial": "Filial A",
        "agente_gestor_grupos_a_que_pertence": ["some_id"]
    }]
    res = compute_dashboard_aggregates(txs)
    assert res["data_quality"]["summary"]["affected_agents_count"] == 0
    assert res["data_quality"]["summary"]["review_only_agents_count"] == 1

def test_data_quality_endpoint_auth_and_schema(monkeypatch):
    config_json = json.dumps([
        {"id": "group_team_1", "name": "Equipe Alpha", "type": "team"}
    ])
    monkeypatch.setenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", config_json)
    
    client = TestClient(app)
    
    # 1. 401 without JWT
    res = client.get("/api/dashboard/full")
    assert res.status_code == 401
    
    # 2. 401 with X-Backend-API-Key (legacy bypass)
    res = client.get("/api/dashboard/full", headers={"X-Backend-API-Key": "some-key"})
    assert res.status_code == 401
    
    # 3. 200 with valid JWT
    from main import verify_backend_api_key
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}
    
    try:
        res = client.get("/api/dashboard/full?data_inicio=2026-01-01&data_fim=2026-06-30")
        assert res.status_code == 200
        
        data = res.json()
        assert "data_quality" in data
        dq = data["data_quality"]
        assert dq["period_basis"] == "ccv"
        assert "summary" in dq
        assert "teams" in dq
        
        raw_json_str = json.dumps(data)
        assert "group_team_1" not in raw_json_str
        assert "email" not in raw_json_str
        assert "telefone" not in raw_json_str
        assert "@" not in raw_json_str
    finally:
        app.dependency_overrides.clear()

def test_directed_mandatory_data_quality_missing_config(monkeypatch):
    from main import compute_dashboard_aggregates
    
    monkeypatch.delenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", raising=False)
    
    txs = [
        # A. Agente A: grupos empty array -> affected, missing_team_assignment
        {
            "transacao_unique_id_pipeimob": "tx_a",
            "agente_gestor": "Agente A",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": []
        },
        # B. Agente B: grupos has ID_NAO_MAPEADO -> review_only, configuration_mapping_required, no missing_team_assignment
        {
            "transacao_unique_id_pipeimob": "tx_b",
            "agente_gestor": "Agente B",
            "agente_gestor_grupo_filial": "Filial B",
            "agente_gestor_grupos_a_que_pertence": ["ID_NAO_MAPEADO"]
        },
        # C. Agente C: grupos is absent, legacy fields empty -> affected, missing_team_assignment
        {
            "transacao_unique_id_pipeimob": "tx_c",
            "agente_gestor": "Agente C",
            "agente_gestor_grupo_filial": "Filial C",
            "agente_gestor_grupos_a_que_pertence": None,
            "agente_gestor_grupos_a_que_pertence1": " ",
            "agente_gestor_grupos_a_que_pertence2": None,
            "agente_gestor_grupos_a_que_pertence3": ""
        }
    ]
    
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    
    assert dq["summary"]["compliant_transactions_count"] == 0
    assert dq["summary"]["affected_transactions_count"] == 2
    assert dq["summary"]["review_only_transactions_count"] == 1
    assert dq["teams"]["reconciliation"]["transactions_reconciled"] is True
    
    issues = {iss["id"]: iss for iss in dq["teams"]["issues"]}
    assert "missing_team_assignment" in issues
    assert "configuration_mapping_required" in issues
    assert issues["missing_team_assignment"]["affected_transactions_count"] == 2
    assert issues["configuration_mapping_required"]["affected_transactions_count"] == 1

def test_data_quality_agents_count_and_composites(monkeypatch):
    from main import compute_dashboard_aggregates
    
    monkeypatch.delenv("PIPEIMOB_OFFICIAL_TEAM_GROUPS_JSON", raising=False)
    
    txs = []
    for i in range(1, 21):
        name = f"Agente {i}"
        groups = [] if i <= 5 else [f"group_{i}"]
        branch = None if 6 <= i <= 10 else f"Filial {i % 3}"
        txs.append({
            "transacao_unique_id_pipeimob": f"tx_agent_{i}",
            "agente_gestor": name,
            "agente_gestor_grupo_filial": branch,
            "agente_gestor_grupos_a_que_pertence": groups
        })
        
    # Same agent same branch -> single key
    txs.append({
        "transacao_unique_id_pipeimob": "tx_same_1",
        "agente_gestor": "Agente 1",
        "agente_gestor_grupo_filial": "Filial 1",
        "agente_gestor_grupos_a_que_pertence": []
    })
    
    # Same agent different branch -> two distinct composite keys
    txs.append({
        "transacao_unique_id_pipeimob": "tx_same_2",
        "agente_gestor": "Agente 1",
        "agente_gestor_grupo_filial": "Filial Diferente",
        "agente_gestor_grupos_a_que_pertence": []
    })
    
    res = compute_dashboard_aggregates(txs)
    dq = res["data_quality"]
    
    assert dq["summary"]["distinct_agents_count"] == 21
    assert dq["teams"]["reconciliation"]["agents_reconciled"] is True
    assert dq["teams"]["reconciliation"]["transactions_reconciled"] is True

def test_dashboard_caching_and_single_flight_scenarios(monkeypatch):
    from main import (
        dashboard_cache, 
        load_transactions_dataset, 
        single_flight_registry,
        generate_dashboard_cache_key
    )
    from mock_data import MOCK_TRANSACTIONS
    
    dashboard_cache.clear()
    monkeypatch.setenv("PIPEIMOB_DATA_MODE", "live")
    monkeypatch.setenv("PIPEIMOB_API_KEY", "test")
    monkeypatch.setenv("PIPEIMOB_SECRET_KEY", "test")
    
    patcher = patch("main.fetch_all_pipeimob_transactions", return_value=(MOCK_TRANSACTIONS, 1))
    patcher.start()
    try:
        loop = asyncio.get_event_loop()
        
        # 1. miss
        res1 = loop.run_until_complete(load_transactions_dataset(
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30",
            request_id="test-miss"
        ))
        mode1, src1, txs1, pages1, status1 = res1
        assert status1 == "miss"
        
        # 2. fresh
        res2 = loop.run_until_complete(load_transactions_dataset(
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30",
            request_id="test-fresh"
        ))
        mode2, src2, txs2, pages2, status2 = res2
        assert status2 == "fresh"
        
        # 3. stale state
        key = generate_dashboard_cache_key(
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30"
        )
        with dashboard_cache.lock:
            val = dashboard_cache.cache[key][0]
            dashboard_cache.cache[key] = (val, time.time() - 10, time.time() + 3000)
            
        res3 = loop.run_until_complete(load_transactions_dataset(
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30",
            request_id="test-stale"
        ))
        mode3, src3, txs3, pages3, status3 = res3
        assert status3 == "stale"
        
        # 4. refresh=True
        dashboard_cache.clear()
        loop.run_until_complete(load_transactions_dataset(
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30",
            request_id="test-miss"
        ))
        
        res4 = loop.run_until_complete(load_transactions_dataset(
            data_inicio_ccv="2026-01-01",
            data_fim_ccv="2026-06-30",
            request_id="test-refresh",
            refresh=True
        ))
        mode4, src4, txs4, pages4, status4 = res4
        assert status4 == "miss"
    finally:
        patcher.stop()

def test_single_flight_concurrent_deduplication(monkeypatch):
    from main import (
        single_flight_registry,
        load_transactions_dataset
    )
    from mock_data import MOCK_TRANSACTIONS
    
    monkeypatch.setenv("PIPEIMOB_DATA_MODE", "live")
    monkeypatch.setenv("PIPEIMOB_API_KEY", "test")
    monkeypatch.setenv("PIPEIMOB_SECRET_KEY", "test")
    
    patcher = patch("main.fetch_all_pipeimob_transactions", return_value=(MOCK_TRANSACTIONS, 1))
    patcher.start()
    try:
        loop = asyncio.get_event_loop()
        
        async def task_wrapper():
            return await load_transactions_dataset(
                data_inicio_ccv="2026-07-01",
                data_fim_ccv="2026-12-31",
                request_id="concurrent-test"
            )
            
        tasks = [task_wrapper(), task_wrapper(), task_wrapper()]
        results = loop.run_until_complete(asyncio.gather(*tasks))
        
        for res in results:
            mode, src, txs, pages, status = res
            assert mode == "live"
            assert len(txs) > 0
    finally:
        patcher.stop()
        
def test_warmup_periods_config(monkeypatch):
    from main import warm_up_dashboard_cache, dashboard_cache, generate_dashboard_cache_key
    from mock_data import MOCK_TRANSACTIONS
    
    monkeypatch.setenv("PIPEIMOB_DATA_MODE", "live")
    monkeypatch.setenv("PIPEIMOB_API_KEY", "test")
    monkeypatch.setenv("PIPEIMOB_SECRET_KEY", "test")
    monkeypatch.setenv("DASHBOARD_WARMUP_PERIODS_JSON", '[{"start_date": "2026-01-01", "end_date": "2026-06-30"}]')
    
    patcher = patch("main.fetch_all_pipeimob_transactions", return_value=(MOCK_TRANSACTIONS, 1))
    patcher.start()
    try:
        dashboard_cache.clear()
        warm_up_dashboard_cache()
        
        key = generate_dashboard_cache_key(data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
        for _ in range(30):
            time.sleep(0.1)
            cached = dashboard_cache.get(key)
            if cached is not None:
                break
                
        assert dashboard_cache.get(key) is not None
    finally:
        patcher.stop()


def test_vgc_phase2_accent_and_fallback():
    from main import compute_dashboard_aggregates
    
    # 1. Check comissionado_imobiliária (with accent) and comissionado_filial
    txs = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"comissionado_imobiliária": True, "comissionado_valor": 3000.0},
                {"comissionado_filial": True, "comissionado_valor": 2000.0},
                {"comissionado_imobiliária": False, "comissionado_valor": 5000.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp = res["commission_financials"]["vgc_composition"]
    assert comp["gralha"]["amount"] == "5000.00"
    assert comp["demais_participantes"]["amount"] == "5000.00"
    assert comp["data_quality"]["valid_split_count"] == 1
    
    # 2. Check fallback comissionado_imobiliaria (without accent)
    txs_fallback = [
        {
            "total_comissao": 5000.0,
            "comissionados": [
                {"comissionado_imobiliaria": True, "comissionado_valor": 2500.0},
                {"comissionado_imobiliaria": False, "comissionado_valor": 2500.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_fallback = compute_dashboard_aggregates(txs_fallback, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_fb = res_fallback["commission_financials"]["vgc_composition"]
    assert comp_fb["gralha"]["amount"] == "2500.00"
    assert comp_fb["demais_participantes"]["amount"] == "2500.00"
    
    # 3. Check matrix and filial included and multiple valid items summed
    txs_multiple = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"comissionado_imobiliária": True, "comissionado_valor": 2000.0},
                {"comissionado_filial": True, "comissionado_valor": 3000.0},
                {"comissionado_imobiliaria": True, "comissionado_valor": 1000.0}, # fallback also counted
                {"comissionado_imobiliária": False, "comissionado_valor": 4000.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_mult = compute_dashboard_aggregates(txs_multiple, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_mult = res_mult["commission_financials"]["vgc_composition"]
    assert comp_mult["gralha"]["amount"] == "6000.00"
    assert comp_mult["demais_participantes"]["amount"] == "4000.00"

    # 4. Absence of filters by CNPJ, nome, tipo
    txs_no_filters = [
        {
            "total_comissao": 8000.0,
            "comissionados": [
                {
                    "comissionado_imobiliária": True, 
                    "comissionado_valor": 4000.0,
                    "cnpj": "12.345.678/0001-99", 
                    "nome": "Qualquer Nome", 
                    "tipo": "Qualquer Tipo",
                    "localizacao": "Qualquer Localizacao"
                },
                {
                    "comissionado_imobiliária": False, 
                    "comissionado_valor": 4000.0
                }
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_nof = compute_dashboard_aggregates(txs_no_filters, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_nof = res_nof["commission_financials"]["vgc_composition"]
    assert comp_nof["gralha"]["amount"] == "4000.00"


def test_vgc_phase2_zero_company_share():
    from main import compute_dashboard_aggregates
    
    # 1. No item matches company/filial -> valid_zero_company_share
    txs = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"comissionado_imobiliária": False, "comissionado_filial": False, "comissionado_valor": 6000.0},
                {"comissionado_imobiliária": False, "comissionado_valor": 4000.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp = res["commission_financials"]["vgc_composition"]
    assert comp["gralha"]["amount"] == "0.00"
    assert comp["demais_participantes"]["amount"] == "10000.00"
    assert comp["data_quality"]["valid_zero_company_share_count"] == 1
    assert comp["data_quality"]["valid_split_count"] == 0


def test_vgc_phase2_array_anomalies():
    from main import compute_dashboard_aggregates
    
    # 1. Missing array (None)
    txs_missing = [
        {
            "total_comissao": 10000.0,
            "comissionados": None,
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_m = compute_dashboard_aggregates(txs_missing, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_m = res_m["commission_financials"]["vgc_composition"]
    assert comp_m["gralha"]["amount"] == "0.00"
    assert comp_m["demais_participantes"]["amount"] == "0.00"
    assert comp_m["unclassified"]["amount"] == "10000.00"
    assert comp_m["data_quality"]["missing_array_count"] == 1
    
    # 2. Malformed array (not a list)
    txs_malformed = [
        {
            "total_comissao": 10000.0,
            "comissionados": "not_a_list",
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_mal = compute_dashboard_aggregates(txs_malformed, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_mal = res_mal["commission_financials"]["vgc_composition"]
    assert comp_mal["gralha"]["amount"] == "0.00"
    assert comp_mal["demais_participantes"]["amount"] == "0.00"
    assert comp_mal["unclassified"]["amount"] == "10000.00"
    assert comp_mal["data_quality"]["malformed_array_count"] == 1

    # 3. Invalid comissionado_valor (e.g. empty string)
    txs_invalid = [
        {
            "total_comissao": 5000.0,
            "comissionados": [
                {"comissionado_imobiliária": True, "comissionado_valor": ""},
                {"comissionado_imobiliária": False, "comissionado_valor": 5000.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_inv = compute_dashboard_aggregates(txs_invalid, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_inv = res_inv["commission_financials"]["vgc_composition"]
    assert comp_inv["gralha"]["amount"] == "0.00"
    assert comp_inv["demais_participantes"]["amount"] == "0.00"
    assert comp_inv["unclassified"]["amount"] == "5000.00"
    assert comp_inv["data_quality"]["invalid_item_value_count"] == 1

    # 4. Negative value
    txs_negative = [
        {
            "total_comissao": 5000.0,
            "comissionados": [
                {"comissionado_imobiliária": True, "comissionado_valor": -100.0},
                {"comissionado_imobiliária": False, "comissionado_valor": 5100.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_neg = compute_dashboard_aggregates(txs_negative, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_neg = res_neg["commission_financials"]["vgc_composition"]
    assert comp_neg["gralha"]["amount"] == "0.00"
    assert comp_neg["demais_participantes"]["amount"] == "0.00"
    assert comp_neg["unclassified"]["amount"] == "5000.00"
    assert comp_neg["data_quality"]["invalid_item_value_count"] == 1


def test_vgc_phase2_reconciliation_tolerance_and_categories():
    from main import compute_dashboard_aggregates
    from decimal import Decimal
    
    # 1. Reconciliation mismatch with difference <= 0.01 (reconciled!)
    txs_tol = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"comissionado_imobiliária": True, "comissionado_valor": 5000.00},
                {"comissionado_imobiliária": False, "comissionado_valor": 5000.01}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_tol = compute_dashboard_aggregates(txs_tol, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_tol = res_tol["commission_financials"]["vgc_composition"]
    assert comp_tol["gralha"]["amount"] == "5000.00"
    assert comp_tol["demais_participantes"]["amount"] == "5000.00"
    assert comp_tol["unclassified"]["amount"] == "0.00"
    assert comp_tol["data_quality"]["reconciliation_mismatch_count"] == 0

    # 2. Reconciliation mismatch with difference > 0.01 (mismatch!)
    txs_mismatch = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"comissionado_imobiliária": True, "comissionado_valor": 5000.00},
                {"comissionado_imobiliária": False, "comissionado_valor": 5000.02} # difference is 0.02
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res_mis = compute_dashboard_aggregates(txs_mismatch, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp_mis = res_mis["commission_financials"]["vgc_composition"]
    assert comp_mis["gralha"]["amount"] == "0.00"
    assert comp_mis["demais_participantes"]["amount"] == "0.00"
    assert comp_mis["unclassified"]["amount"] == "10000.00"
    assert comp_mis["data_quality"]["reconciliation_mismatch_count"] == 1
    assert comp_mis["data_quality"]["reconciliation_difference"] == "0.02"

    # 3. Verify math: total = gralha + demais_participantes + unclassified
    tot_val = Decimal(comp_mis["total"]["amount"])
    gralha_val = Decimal(comp_mis["gralha"]["amount"])
    demais_val = Decimal(comp_mis["demais_participantes"]["amount"])
    unclass_val = Decimal(comp_mis["unclassified"]["amount"])
    assert tot_val == gralha_val + demais_val + unclass_val


def test_vgc_phase2_deprecated_alias_corretores_equipe():
    from main import compute_dashboard_aggregates
    
    txs = [
        {
            "total_comissao": 10000.0,
            "comissionados": [
                {"comissionado_imobiliária": True, "comissionado_valor": 3000.0},
                {"comissionado_imobiliária": False, "comissionado_valor": 7000.0}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp = res["commission_financials"]["vgc_composition"]
    assert "corretores_equipe" in comp
    assert comp["corretores_equipe"] == comp["demais_participantes"]


def test_vgc_phase2_payment_receipt_semantic_rules():
    from main import compute_dashboard_aggregates
    from zoneinfo import ZoneInfo
    from datetime import datetime, timedelta
    
    sp_tz = ZoneInfo("America/Sao_Paulo")
    now_sp = datetime.now(sp_tz)
    today_str = now_sp.strftime("%Y-%m-%d")
    yesterday_str = (now_sp - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_str = (now_sp + timedelta(days=1)).strftime("%Y-%m-%d")
    
    txs = [
        # 1. data_pagamento_comissao_prevista valid and past -> received
        {
            "total_comissao": 1000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.0}],
            "data_pagamento_comissao_prevista": yesterday_str,
            "data_assinatura_ccv": "2026-03-15"
        },
        # 2. data_pagamento_comissao_prevista future -> unknown
        {
            "total_comissao": 2000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 2000.0}],
            "data_pagamento_comissao_prevista": tomorrow_str,
            "data_assinatura_ccv": "2026-03-15"
        },
        # 3. data_pagamento_comissao_prevista missing/empty -> pending
        {
            "total_comissao": 3000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 3000.0}],
            "data_pagamento_comissao_prevista": "",
            "data_assinatura_ccv": "2026-03-15"
        },
        # 4. data_pagamento_comissao_prevista invalid date format -> unknown
        {
            "total_comissao": 4000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 4000.0}],
            "data_pagamento_comissao_prevista": "not-a-date",
            "data_assinatura_ccv": "2026-03-15"
        },
        # 5. data_pagamento_comissao (scheduled/prevista API) NOT used to determine receipt
        {
            "total_comissao": 5000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 5000.0}],
            "data_pagamento_comissao_prevista": None,
            "data_recebimento_comissao": None,
            "data_pagamento_comissao": yesterday_str, # should not be used!
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    assert financials["received"]["transaction_count"] == 1
    assert financials["pending"]["transaction_count"] == 2 # item 3 and item 5
    assert financials["unknown"]["transaction_count"] == 2 # item 2 and item 4


def test_vgc_phase2_no_float_or_pii():
    from main import compute_dashboard_aggregates, extract_commission_split
    from decimal import Decimal
    
    tx = {
        "total_comissao": 10000.00,
        "comissionados": [
            {
                "comissionado_imobiliária": True, 
                "comissionado_valor": 4000.00,
                "nome_colaborador": "Maria Silva",
                "cnpj_colaborador": "12.345.678/0001-00"
            },
            {
                "comissionado_imobiliária": False, 
                "comissionado_valor": 6000.00,
                "nome_colaborador": "João Souza"
            }
        ],
        "data_assinatura_ccv": "2026-03-15"
    }
    
    # Assert extract_commission_split does not use float internally
    ext = extract_commission_split(tx)
    assert isinstance(ext.gralha_amount, Decimal)
    assert isinstance(ext.all_participants_amount, Decimal)
    
    res = compute_dashboard_aggregates([tx], data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp = res["commission_financials"]["vgc_composition"]
    
    # Assert no sensitive personal data (PII) is exposed in contract
    raw_response_str = json.dumps(comp)
    assert "Maria Silva" not in raw_response_str
    assert "João Souza" not in raw_response_str
    assert "12.345.678/0001-00" not in raw_response_str


def test_vgc_phase2_auth_and_other_metrics():
    from main import app, verify_backend_api_key
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    
    client = TestClient(app)
    
    # 1. 401 without JWT
    res = client.get("/api/dashboard/full")
    assert res.status_code == 401
    
    # 2. 401 with legacy headers
    res_legacy = client.get("/api/dashboard/full", headers={"X-Backend-API-Key": "some-key"})
    assert res_legacy.status_code == 401
    
    # Override auth for JWT
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated", "email": "test@test.com"}
    
    try:
        # Mock transactions pagination inside load_transactions_dataset
        with patch("main.fetch_all_pipeimob_transactions") as mock_fetch:
            mock_fetch.return_value = ([], 1)
            res_ok = client.get("/api/dashboard/full?data_inicio_ccv=2026-01-01")
            assert res_ok.status_code != 401
    finally:
        app.dependency_overrides.clear()


def test_vgc_priority_prevista_wins():
    from main import compute_dashboard_aggregates
    from zoneinfo import ZoneInfo
    from datetime import datetime, timedelta
    
    sp_tz = ZoneInfo("America/Sao_Paulo")
    now_sp = datetime.now(sp_tz)
    yesterday_str = (now_sp - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_str = (now_sp + timedelta(days=1)).strftime("%Y-%m-%d")
    
    txs = [
        {
            "total_comissao": 10000.0,
            "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 10000.0}],
            # data_pagamento_comissao_prevista (yesterday/received) wins over data_recebimento_comissao (tomorrow/unknown)
            "data_pagamento_comissao_prevista": yesterday_str,
            "data_recebimento_comissao": tomorrow_str,
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    # Should be classified as received (because data_pagamento_comissao_prevista wins and is in the past)
    assert financials["received"]["transaction_count"] == 1
    assert financials["unknown"]["transaction_count"] == 0
    assert financials["receipt_date_sources"]["data_pagamento_comissao_prevista"] == 1
    assert financials["receipt_date_sources"]["data_recebimento_comissao"] == 0


def test_vgc_receipt_missing_and_absent_classifies_as_pending():
    from main import compute_dashboard_aggregates
    txs = [
        # 1. missing key
        {"total_comissao": 1000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.0}], "data_assinatura_ccv": "2026-03-15"},
        # 2. None value
        {"total_comissao": 2000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 2000.0}], "data_pagamento_comissao_prevista": None, "data_assinatura_ccv": "2026-03-15"},
        # 3. empty string
        {"total_comissao": 3000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 3000.0}], "data_pagamento_comissao_prevista": "", "data_assinatura_ccv": "2026-03-15"},
        # 4. whitespace string
        {"total_comissao": 4000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 4000.0}], "data_pagamento_comissao_prevista": "   ", "data_assinatura_ccv": "2026-03-15"},
        # 5. "None" string
        {"total_comissao": 5000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 5000.0}], "data_pagamento_comissao_prevista": "None", "data_assinatura_ccv": "2026-03-15"},
        # 6. "null" string
        {"total_comissao": 6000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 6000.0}], "data_pagamento_comissao_prevista": "null", "data_assinatura_ccv": "2026-03-15"},
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    assert financials["pending_transactions_count"] == 6
    assert financials["received_transactions_count"] == 0
    assert financials["unknown_transactions_count"] == 0
    assert financials["receipt_data_quality"]["missing_date_count"] == 6


def test_vgc_receipt_unknown_only_for_invalid_or_future():
    from main import compute_dashboard_aggregates
    txs = [
        # 1. Future date
        {"total_comissao": 1000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.0}], "data_pagamento_comissao_prevista": "2050-01-01", "data_assinatura_ccv": "2026-03-15"},
        # 2. Invalid date format/value
        {"total_comissao": 2000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 2000.0}], "data_pagamento_comissao_prevista": "invalid-date", "data_assinatura_ccv": "2026-03-15"},
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    assert financials["unknown_transactions_count"] == 2
    assert financials["pending_transactions_count"] == 0
    assert financials["received_transactions_count"] == 0
    assert financials["receipt_data_quality"]["future_date_count"] == 1
    assert financials["receipt_data_quality"]["invalid_date_count"] == 1


def test_vgc_calculation_status_validated_vs_partial():
    from main import compute_dashboard_aggregates
    
    # 1. Fully reconciled -> validated
    txs_valid = [
        {"total_comissao": 1000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.0}], "data_assinatura_ccv": "2026-03-15"}
    ]
    res1 = compute_dashboard_aggregates(txs_valid, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    assert res1["commission_financials"]["vgc_composition"]["calculation_status"] == "validated"
    
    # 2. Unclassified > 0 -> partial
    txs_unclassified = [
        {"total_comissao": 1000.0, "comissionados": None, "data_assinatura_ccv": "2026-03-15"}
    ]
    res2 = compute_dashboard_aggregates(txs_unclassified, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    assert res2["commission_financials"]["vgc_composition"]["calculation_status"] == "partial"
    
    # 3. Mismatch -> partial
    txs_mismatch = [
        {"total_comissao": 1000.0, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 900.0}], "data_assinatura_ccv": "2026-03-15"}
    ]
    res3 = compute_dashboard_aggregates(txs_mismatch, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    assert res3["commission_financials"]["vgc_composition"]["calculation_status"] == "partial"


def test_vgc_sum_and_reconciliation_difference_016():
    from main import compute_dashboard_aggregates
    txs = [
        # Sums to 1000.16 commission split but total_comissao is 1000.00 -> difference is 0.16
        {
            "total_comissao": 1000.00,
            "comissionados": [
                {"comissionado_imobiliaria": True, "comissionado_valor": 500.08},
                {"comissionado_imobiliaria": False, "comissionado_valor": 500.08}
            ],
            "data_assinatura_ccv": "2026-03-15"
        }
    ]
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    comp = res["commission_financials"]["vgc_composition"]
    assert comp["data_quality"]["reconciliation_mismatch_count"] == 1
    assert comp["data_quality"]["reconciliation_difference"] == "0.16"
    assert comp["unclassified"]["amount"] == "1000.00"
    assert comp["gralha"]["amount"] == "0.00"
    assert comp["demais_participantes"]["amount"] == "0.00"
    assert comp["calculation_status"] == "partial"


def test_vgc_receipt_data_quality_comprehensive_closure():
    from main import compute_dashboard_aggregates
    
    txs = [
        # 1. missing key (missing)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_assinatura_ccv": "2026-03-15"},
        # 2. None value (missing)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": None, "data_assinatura_ccv": "2026-03-15"},
        # 3. empty string (missing)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "", "data_assinatura_ccv": "2026-03-15"},
        # 4. whitespace string (missing)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "   ", "data_assinatura_ccv": "2026-03-15"},
        # 5. "None" string (missing)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "None", "data_assinatura_ccv": "2026-03-15"},
        # 6. "null" string (missing)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "null", "data_assinatura_ccv": "2026-03-15"},
        # 7. valid past date (received)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "2026-03-15", "data_assinatura_ccv": "2026-03-15"},
        # 8. equal to reference date (received)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "2026-06-30", "data_assinatura_ccv": "2026-03-15"},
        # 9. valid future date (future -> unknown)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "2050-01-01", "data_assinatura_ccv": "2026-03-15"},
        # 10. invalid date format (invalid -> unknown)
        {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "not-a-date", "data_assinatura_ccv": "2026-03-15"},
    ]
    
    res = compute_dashboard_aggregates(txs, data_inicio_ccv="2026-01-01", data_fim_ccv="2026-06-30")
    financials = res["commission_financials"]
    q = financials["receipt_data_quality"]
    
    # Assert closures
    assert q["missing_date_count"] == 6
    assert q["received_date_count"] == 2
    assert q["future_date_count"] == 1
    assert q["invalid_date_count"] == 1
    
    records_count = len(txs)
    # Closure of quality counts: received + missing + invalid + future == records_count
    assert q["received_date_count"] + q["missing_date_count"] + q["invalid_date_count"] + q["future_date_count"] == records_count
    
    # Closure mapping to transaction counts
    assert financials["received_transactions_count"] == q["received_date_count"]
    assert financials["pending_transactions_count"] == q["missing_date_count"]
    assert financials["unknown_transactions_count"] == q["invalid_date_count"] + q["future_date_count"]
    
    # Closure of status counts: received + pending + unknown == records_count
    assert financials["received_transactions_count"] + financials["pending_transactions_count"] + financials["unknown_transactions_count"] == records_count
