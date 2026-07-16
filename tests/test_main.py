import os
import sys
import json
from datetime import datetime, timezone
from fastapi.testclient import TestClient

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Force development environment for local localhost CORS tests
os.environ["APP_ENV"] = "development"
os.environ["ALLOWED_ORIGINS"] = "https://lovable-test-origin.app"
os.environ["BACKEND_API_KEY"] = "test_backend_key"
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
    os.environ["APP_ENV"] = "production"
    os.environ.pop("PIPEIMOB_DATA_MODE", None)
    
    # Endpoints must fail with 503 while unconfigured, never returning demo data silently
    response = client.get("/api/transactions", headers={"X-Backend-API-Key": "test_backend_key"})
    assert response.status_code == 503
    assert "Configuration pending" in response.json()["detail"]
    
    response = client.get("/api/dashboard/summary", headers={"X-Backend-API-Key": "test_backend_key"})
    assert response.status_code == 503
    assert "Configuration pending" in response.json()["detail"]
    
    os.environ["APP_ENV"] = "development"

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
    err_res = client.get(
        "/api/transactions?data_inicio_criacao=2026-01-01",
        headers={"X-Backend-API-Key": "test_backend_key"}
    )
    assert err_res.status_code == 503
    err_body = err_res.json()
    for val in err_body.values():
        val_str = str(val).lower()
        assert "api_key" not in val_str
        assert "secret_key" not in val_str
        assert "token" not in val_str
    
    os.environ["APP_ENV"] = "development"

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


def test_server_to_server_bypass_key():
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    unauth_client = TestClient(app)
    
    # Passing X-Backend-API-Key server-to-server bypass -> HTTP 200 OK
    res = unauth_client.get("/api/dashboard/summary", headers={"X-Backend-API-Key": "test_backend_key"})
    assert res.status_code == 200


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
                # Ignore the default server-to-server mock email in the response if it pops up under manager or other fields,
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






