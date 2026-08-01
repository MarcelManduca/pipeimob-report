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

def test_cors_preflight_options_patch():
    headers = {
        "Origin": "https://lovable-test-origin.app",
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    response = client.options("/api/contracts-control/deals/tx_123/manual-data", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://lovable-test-origin.app"
    assert "PATCH" in response.headers.get("access-control-allow-methods", "")
    assert "authorization" in response.headers.get("access-control-allow-headers", "").lower()
    assert "content-type" in response.headers.get("access-control-allow-headers", "").lower()

def test_cors_preflight_options_patch_unauthorized():
    headers = {
        "Origin": "https://unauthorized-domain.com",
        "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    response = client.options("/api/contracts-control/deals/tx_123/manual-data", headers=headers)
    assert "access-control-allow-origin" not in response.headers

def test_cors_preflight_summary_lovable_preview():
    headers = {
        "Origin": "https://preview--happy-data-hugger.lovable.app",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    response = client.options("/api/contracts-control/summary", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://preview--happy-data-hugger.lovable.app"
    assert "GET" in response.headers.get("access-control-allow-methods", "")
    assert "authorization" in response.headers.get("access-control-allow-headers", "").lower()

def test_cors_preflight_import_preview_lovable_branch():
    headers = {
        "Origin": "https://my-awesome-branch-123--happy-data-hugger.lovable.app",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    response = client.options("/api/contracts-control/imports/responsibles/preview", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://my-awesome-branch-123--happy-data-hugger.lovable.app"
    assert "POST" in response.headers.get("access-control-allow-methods", "")
    assert "authorization" in response.headers.get("access-control-allow-headers", "").lower()
    assert "content-type" in response.headers.get("access-control-allow-headers", "").lower()

def test_cors_preflight_unauthorized_lovable_app():
    headers = {
        "Origin": "https://preview--other-app-name.lovable.app",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    }
    response = client.options("/api/contracts-control/summary", headers=headers)
    assert "access-control-allow-origin" not in response.headers

def test_cors_preflight_unauthorized_external_origin():
    headers = {
        "Origin": "https://evil-hacker-domain.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    response = client.options("/api/contracts-control/imports/responsibles/preview", headers=headers)
    assert "access-control-allow-origin" not in response.headers

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
    assert "Failed to authenticate" in response.json()["detail"] or "Authentication payload" in response.json()["detail"] or "unreachable" in response.json()["detail"]

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
        rs256_mock_token = "eyJhbGciOiJSUzI1NiIsImtpZCI6Im1vY2tfa2lkIn0.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
        jwks_client = TestClient(app, headers={"Authorization": f"Bearer {rs256_mock_token}"})
        res = jwks_client.get("/api/dashboard/summary")
        assert res.status_code == 503
        assert res.json()["error_code"] in ["supabase_jwks_unavailable", "supabase_jwks_invalid"]
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

        # Create token signed with disallowed algorithm HS512
        disallowed_token = create_mock_jwt(alg="HS512")
        disallowed_client = TestClient(app, headers={"Authorization": f"Bearer {disallowed_token}"})

        res = disallowed_client.get("/api/dashboard/summary")
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
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    try:
        mock_jwk_set = MagicMock()
        mock_key = MagicMock()
        mock_key.kid = "other_kid"
        mock_jwk_set.keys = [mock_key]
        mock_client = MagicMock()
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_client.get_signing_key_from_jwt.side_effect = Exception("Signing key not found")
        mock_get_jwk_client.return_value = mock_client

        token = "eyJhbGciOiJSUzI1NiIsImtpZCI6InVua25vd25fa2lkIn0.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 401
        assert "Invalid or expired access token." in res.json()["detail"]
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

def test_jwt_token_aleatorio_retorna_401():
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
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
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        mock_jwk_set = MagicMock()
        mock_key = MagicMock()
        mock_key.kid = "mock_kid"
        mock_key.key = "dummy_public_key_which_fails_verification"
        mock_jwk_set.keys = [mock_key]
        mock_client = MagicMock()
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_client.get_signing_key_from_jwt.return_value = mock_key
        mock_get_jwk_client.return_value = mock_client

        token = "eyJhbGciOiJSUzI1NiIsImtpZCI6Im1vY2tfa2lkIn0.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 401
        assert "Invalid or expired access token." in res.json()["detail"]
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

@patch("main.get_jwk_client")
def test_jwks_offline_retorna_503(mock_get_jwk_client):
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
    from jwt.exceptions import PyJWKClientConnectionError
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        mock_client = MagicMock()
        mock_client.get_jwk_set.side_effect = PyJWKClientConnectionError("Connection timed out")
        mock_get_jwk_client.return_value = mock_client

        token = "eyJhbGciOiJSUzI1NiIsImtpZCI6Im1vY2tfa2lkIn0.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 503
        assert res.json()["error_code"] == "supabase_jwks_unavailable"
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

@patch("main.get_jwk_client")
def test_jwks_vazio_retorna_503(mock_get_jwk_client):
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        mock_client = MagicMock()
        mock_jwk_set = MagicMock()
        mock_jwk_set.keys = []
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_get_jwk_client.return_value = mock_client

        token = "eyJhbGciOiJSUzI1NiIsImtpZCI6Im1vY2tfa2lkIn0.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 503
        assert res.json()["error_code"] == "supabase_jwks_invalid"

    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

@patch("main.get_jwk_client")
@patch("jwt.decode")
def test_jwt_valido_retorna_200(mock_jwt_decode, mock_get_jwk_client):
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
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

        token = "eyJhbGciOiJSUzI1NiIsImtpZCI6Im1vY2tfa2lkIn0.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
        test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
        res = test_client.get("/api/dashboard/summary")
        assert res.status_code == 200
    finally:
        os.environ.pop("SUPABASE_JWKS_URL", None)

def test_jwt_sem_kid_retorna_401():
    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]
    os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
    try:
        # Create token without kid header for RS256
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
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

    assert financials["unknown"]["total"] == "3000.00"
    assert financials["unknown"]["gralha"] == "1000.00"
    assert financials["unknown"]["demais_participantes"] == "2000.00"
    assert financials["unknown"]["transaction_count"] == 1
    assert financials["unknown"]["invalid_date_count"] == 1
    assert financials["unknown"]["future_date_count"] == 0


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
    assert financials["pending"]["without_date_count"] == 2
    assert financials["pending"]["transaction_count"] == 2
    assert financials["unknown"]["transaction_count"] == 2
    assert financials["unknown"]["invalid_date_count"] == 1
    assert financials["unknown"]["future_date_count"] == 1

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

def test_single_flight_diagnostics_scenarios():
    import pytest
    from main import AsyncSingleFlightRegistry

    loop = asyncio.get_event_loop()

    # 1. Owner succeeding
    async def owner_succeeds():
        registry = AsyncSingleFlightRegistry()
        async def fetch():
            await asyncio.sleep(0.01)
            return "ok"
        res = await registry.execute("k1", fetch)
        assert res == "ok"
        # 6. finally block cleanup: key is removed
        assert not await registry.is_running("k1")
    loop.run_until_complete(owner_succeeds())

    # 2. Owner failing
    async def owner_fails():
        registry = AsyncSingleFlightRegistry()
        async def fetch():
            raise ValueError("custom_error")
        with pytest.raises(ValueError, match="custom_error"):
            await registry.execute("k2", fetch)
        # finally block cleanup on failure: key is removed
        assert not await registry.is_running("k2")
    loop.run_until_complete(owner_fails())

    # 3. Waiter receiving success
    async def waiter_receives_success():
        registry = AsyncSingleFlightRegistry()
        barrier = asyncio.Event()

        async def fetch():
            await barrier.wait()
            return "shared_value"

        async def run_owner():
            return await registry.execute("k3", fetch)

        async def run_waiter():
            # Wait a tiny bit to ensure leader task is registered
            await asyncio.sleep(0.005)
            return await registry.execute("k3", None)

        task1 = asyncio.create_task(run_owner())
        task2 = asyncio.create_task(run_waiter())

        # Unblock the owner
        await asyncio.sleep(0.01)
        barrier.set()

        res1, res2 = await asyncio.gather(task1, task2)
        assert res1 == "shared_value"
        assert res2 == "shared_value"
        assert not await registry.is_running("k3")
    loop.run_until_complete(waiter_receives_success())

    # 4. Waiter receiving failure
    async def waiter_receives_failure():
        registry = AsyncSingleFlightRegistry()
        barrier = asyncio.Event()

        async def fetch():
            await barrier.wait()
            raise RuntimeError("shared_error")

        async def run_owner():
            return await registry.execute("k4", fetch)

        async def run_waiter():
            await asyncio.sleep(0.005)
            return await registry.execute("k4", None)

        task1 = asyncio.create_task(run_owner())
        task2 = asyncio.create_task(run_waiter())

        await asyncio.sleep(0.01)
        barrier.set()

        # Both owner and waiter should raise the same exception
        with pytest.raises(RuntimeError, match="shared_error"):
            await task1
        with pytest.raises(RuntimeError, match="shared_error"):
            await task2
        assert not await registry.is_running("k4")
    loop.run_until_complete(waiter_receives_failure())

    # 5. Waiting timeout
    async def waiter_timeout():
        registry = AsyncSingleFlightRegistry()
        async def fetch():
            await asyncio.sleep(1.0)
            return "never_reached"

        async def run_waiter():
            await asyncio.sleep(0.005)
            # Timeout after 0.05 seconds while waiting for the leader
            await asyncio.wait_for(registry.execute("k5", None), timeout=0.05)

        task1 = asyncio.create_task(registry.execute("k5", fetch))
        task2 = asyncio.create_task(run_waiter())

        with pytest.raises(asyncio.TimeoutError):
            await task2

        # Clean up
        task1.cancel()
        try:
            await task1
        except asyncio.CancelledError:
            pass
    loop.run_until_complete(waiter_timeout())

    # 7. Call cancelled
    async def call_cancelled():
        registry = AsyncSingleFlightRegistry()
        fetch_cancelled = False

        async def fetch():
            nonlocal fetch_cancelled
            try:
                await asyncio.sleep(1.0)
                return "completed"
            except asyncio.CancelledError:
                fetch_cancelled = True
                raise

        task = asyncio.create_task(registry.execute("k6", fetch))
        await asyncio.sleep(0.005)
        # Cancel the task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Verify fetch coroutine was NOT cancelled if shielded
        await asyncio.sleep(0.01) # let loop run
        assert fetch_cancelled is False
    loop.run_until_complete(call_cancelled())

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

    # Assert pending and unknown dictionary structures
    assert "future_date_count" not in financials["pending"]
    assert financials["pending"]["without_date_count"] == q["missing_date_count"]
    assert financials["unknown"]["invalid_date_count"] == q["invalid_date_count"]
    assert financials["unknown"]["future_date_count"] == q["future_date_count"]

    # Closure of status counts: received + pending + unknown == records_count
    assert financials["received_transactions_count"] + financials["pending_transactions_count"] + financials["unknown_transactions_count"] == records_count


def test_dashboard_full_endpoint_serialization_and_restrictions():
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key, DASHBOARD_CACHE_VERSION
    from unittest.mock import patch
    import os
    import json

    client = TestClient(app)

    # Bypass auth
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated", "email": "test@test.com"}

    try:
        mock_txs = [
            # received
            {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "2026-03-15", "data_assinatura_ccv": "2026-03-15", "valor_contrato": 50000.0, "transacao_unique_id_pipeimob": "tx1", "codigo_contrato": "code1", "agente_gestor": "Manager A"},
            # missing (pending)
            {"total_comissao": 2000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 2000.00}], "data_pagamento_comissao_prevista": None, "data_assinatura_ccv": "2026-03-15", "valor_contrato": 100000.0, "transacao_unique_id_pipeimob": "tx2", "codigo_contrato": "code2", "agente_gestor": "Manager B"},
            # future (unknown)
            {"total_comissao": 3000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 3000.00}], "data_pagamento_comissao_prevista": "2050-01-01", "data_assinatura_ccv": "2026-03-15", "valor_contrato": 150000.0, "transacao_unique_id_pipeimob": "tx3", "codigo_contrato": "code3", "agente_gestor": "Manager C"},
            # invalid (unknown)
            {"total_comissao": 4000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 4000.00}], "data_pagamento_comissao_prevista": "invalid", "data_assinatura_ccv": "2026-03-15", "valor_contrato": 200000.0, "transacao_unique_id_pipeimob": "tx4", "codigo_contrato": "code4", "agente_gestor": "Manager D"}
        ]

        # Test with EXPOSE_RAW_TRANSACTIONS = false (default)
        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {
                 "PIPEIMOB_DATA_MODE": "live",
                 "PIPEIMOB_API_KEY": "mock_key",
                 "PIPEIMOB_SECRET_KEY": "mock_secret",
                 "EXPOSE_RAW_TRANSACTIONS": "false"
             }):

            res = client.get("/api/dashboard/full?data_inicio_ccv=2026-01-01&data_fim_ccv=2026-06-30")
            assert res.status_code == 200
            data = res.json()

            # Assert cache version changed check
            assert DASHBOARD_CACHE_VERSION == "sales-cycle-v6-vgc-pending-unknown-fix"

            # Assert receipt_data_quality is present and serialized correctly
            financials = data.get("commission_financials")
            assert financials is not None
            q = financials.get("receipt_data_quality")
            assert q is not None

            # Check counts
            assert q["received_date_count"] == 1
            assert q["missing_date_count"] == 1
            assert q["future_date_count"] == 1
            assert q["invalid_date_count"] == 1

            # Closure of four counts
            assert q["received_date_count"] + q["missing_date_count"] + q["invalid_date_count"] + q["future_date_count"] == 4

            # Assert pending dict contains only total, gralha, demais_participantes, transaction_count, without_date_count
            pending_dict = financials.get("pending")
            assert pending_dict is not None
            assert "future_date_count" not in pending_dict
            assert pending_dict["without_date_count"] == q["missing_date_count"]
            assert pending_dict["transaction_count"] == q["missing_date_count"]

            # Assert unknown dict contains total, gralha, demais_participantes, transaction_count, invalid_date_count, future_date_count
            unknown_dict = financials.get("unknown")
            assert unknown_dict is not None
            assert unknown_dict["invalid_date_count"] == q["invalid_date_count"]
            assert unknown_dict["future_date_count"] == q["future_date_count"]
            assert unknown_dict["transaction_count"] == unknown_dict["invalid_date_count"] + unknown_dict["future_date_count"]

            # Reconciliations with transaction counts
            assert financials["received"]["transaction_count"] == q["received_date_count"]
            assert financials["pending"]["transaction_count"] == q["missing_date_count"]
            assert financials["unknown"]["transaction_count"] == q["invalid_date_count"] + q["future_date_count"]

            # Closure of status counts: received + pending + unknown == records_count
            assert financials["received_transactions_count"] == q["received_date_count"]
            assert financials["pending_transactions_count"] == q["missing_date_count"]
            assert financials["unknown_transactions_count"] == q["invalid_date_count"] + q["future_date_count"]

            # Check disclaimer text is semantic and updated
            disclaimer = financials.get("disclaimer")
            assert disclaimer is not None
            assert "data válida até a data de referência (as_of_date): recebido;" in disclaimer
            assert "data ausente: pendente;" in disclaimer
            assert "data futura ou inválida: desconhecido;" in disclaimer
            assert "não comprova a liquidação" in disclaimer
            assert "datas futuras são pendentes" not in disclaimer

            # Check that individual transaction records are omitted when EXPOSE_RAW_TRANSACTIONS=false
            commissions_data = data.get("commissions")
            assert commissions_data is not None
            assert commissions_data.get("commissions") == [] # MUST BE EMPTY!

            # Ensure no individual transactional fields exist anywhere in the payload
            payload_str = json.dumps(data)
            assert "transaction_id" not in payload_str
            assert "contract_code" not in payload_str
            assert "code1" not in payload_str
            assert "code2" not in payload_str

            # Verify no regressions in financial composition values
            assert float(financials["vgc_total"]) == 10000.00
            assert float(financials["vgc_composition"]["gralha"]["amount"]) == 10000.00
            assert float(financials["vgc_composition"]["demais_participantes"]["amount"]) == 0.00

        # Test with EXPOSE_RAW_TRANSACTIONS = true (for local controlled environment)
        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {
                 "PIPEIMOB_DATA_MODE": "live",
                 "PIPEIMOB_API_KEY": "mock_key",
                 "PIPEIMOB_SECRET_KEY": "mock_secret",
                 "EXPOSE_RAW_TRANSACTIONS": "true"
             }):

            res_exposed = client.get("/api/dashboard/full?data_inicio_ccv=2026-01-01&data_fim_ccv=2026-06-30")
            assert res_exposed.status_code == 200
            data_exposed = res_exposed.json()
            comm_list = data_exposed.get("commissions", {}).get("commissions", [])
            assert len(comm_list) == 4
            assert comm_list[0]["contract_code"] == "code1"
            assert comm_list[0]["transaction_id"] == "tx1"

    finally:
        app.dependency_overrides.clear()


# ======================================================================
# CONTRACTS CONTROL (SECRETARIA DE VENDAS) BI MODULE TESTS
# ======================================================================

def test_contracts_control_strict_date_parsing():
    from main import parse_date_to_date_obj
    from datetime import date

    # Valid YYYY-MM-DD
    assert parse_date_to_date_obj("2026-05-15") == date(2026, 5, 15)
    # Valid DD/MM/YYYY
    assert parse_date_to_date_obj("25/12/2026") == date(2026, 12, 25)
    # ISO timestamp
    assert parse_date_to_date_obj("2026-07-24T18:00:00Z") == date(2026, 7, 24)
    # Empty/Null/Whitespace
    assert parse_date_to_date_obj(None) is None
    assert parse_date_to_date_obj("   ") is None
    assert parse_date_to_date_obj("None") is None
    assert parse_date_to_date_obj("null") is None
    # Invalid formats
    assert parse_date_to_date_obj("2026/05/15") is None
    assert parse_date_to_date_obj("not-a-date") is None
    assert parse_date_to_date_obj("31-02-2026") is None

def test_contracts_control_classification_logic():
    from main import classify_contracts_control_process
    from datetime import date

    as_of = date(2026, 7, 24)
    end_date = date(2026, 6, 30)

    # Completed in period
    tx1 = {"data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25"}
    res1 = classify_contracts_control_process(tx1, as_of, end_date)
    assert res1["current_status"] == "completed"
    assert res1["status_at_period_end"] == "completed"
    assert res1["duration_days"] == 15
    assert res1["current_aging_days"] is None
    assert res1["aging_days_at_period_end"] is None

    # In progress at period end, completed currently (completed after period end)
    tx2 = {"data_inicio_venda": "2026-06-10", "data_contrato": "2026-07-15"}
    res2 = classify_contracts_control_process(tx2, as_of, end_date)
    assert res2["current_status"] == "completed"
    assert res2["status_at_period_end"] == "in_progress"
    assert res2["duration_days"] == 35
    assert res2["current_aging_days"] is None
    assert res2["aging_days_at_period_end"] == 20  # 2026-06-30 - 2026-06-10

    # Currently in progress (no contract date)
    tx3 = {"data_inicio_venda": "2026-06-10", "data_contrato": None}
    res3 = classify_contracts_control_process(tx3, as_of, end_date)
    assert res3["current_status"] == "in_progress"
    assert res3["status_at_period_end"] == "in_progress"
    assert res3["current_aging_days"] == 44  # 2026-07-24 - 2026-06-10
    assert res3["aging_days_at_period_end"] == 20

    # Data Issues
    # Negative duration
    tx4 = {"data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-05"}
    res4 = classify_contracts_control_process(tx4, as_of, end_date)
    assert res4["current_status"] == "data_issue"
    assert res4["status_at_period_end"] == "data_issue"
    assert "negative_duration" in res4["data_quality_flags"]

    # Future start date relative to period end
    tx5 = {"data_inicio_venda": "2026-07-05", "data_contrato": None}
    res5 = classify_contracts_control_process(tx5, as_of, end_date)
    assert res5["status_at_period_end"] == "future"
    # But currently valid in_progress (since start_date <= as_of)
    assert res5["current_status"] == "in_progress"

    # Missing / Invalid start date
    tx6 = {"data_inicio_venda": "", "data_contrato": "2026-06-10"}
    res6 = classify_contracts_control_process(tx6, as_of, end_date)
    assert res6["current_status"] == "data_issue"
    assert "missing_start_date" in res6["data_quality_flags"]

def test_contracts_control_deduplication_and_conflicts():
    from main import deduplicate_contracts_control_dataset

    # No duplicates
    dataset = [
        {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123"},
        {"transacao_unique_id_pipeimob": "tx2", "codigo_imovel": "456"}
    ]
    uniq, dups, conflicts = deduplicate_contracts_control_dataset(dataset)
    assert len(uniq) == 2
    assert dups == 0
    assert conflicts == 0

    # Duplicates with same values
    dataset_dup = [
        {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-01-01"},
        {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-01-01"},
        {"transacao_unique_id_pipeimob": "tx2", "codigo_imovel": "456"}
    ]
    uniq_dup, dups_dup, conflicts_dup = deduplicate_contracts_control_dataset(dataset_dup)
    assert len(uniq_dup) == 2
    assert dups_dup == 1
    assert conflicts_dup == 0

    # Duplicates with conflicts
    dataset_conflict = [
        {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-01-01"},
        {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "999", "data_inicio_venda": "2026-01-01"},  # different property code
        {"transacao_unique_id_pipeimob": "tx2", "codigo_imovel": "456"}
    ]
    uniq_cf, dups_cf, conflicts_cf = deduplicate_contracts_control_dataset(dataset_conflict)
    assert len(uniq_cf) == 2
    assert dups_cf == 1
    assert conflicts_cf == 1

def test_contracts_control_backlog_math_and_timeline():
    from main import compute_contracts_control_data

    # Transactions to verify backlog logic
    # start_date: 2026-06-01, end_date: 2026-06-30
    dataset = [
        # 1. Opening backlog: started in May, still open at June start, completed on June 15
        {"transacao_unique_id_pipeimob": "tx1", "data_inicio_venda": "2026-05-10", "data_contrato": "2026-06-15", "agente_gestor": "Manager A", "financiamento": True, "codigo_imovel": "i1"},
        # 2. Opening backlog boundary condition: started in May, signed exactly on June 1 (start_date)
        {"transacao_unique_id_pipeimob": "tx2", "data_inicio_venda": "2026-05-15", "data_contrato": "2026-06-01", "agente_gestor": "Manager A", "financiamento": False, "codigo_imovel": "i2"},
        # 3. Started in period (June 10), still open at end of June
        {"transacao_unique_id_pipeimob": "tx3", "data_inicio_venda": "2026-06-10", "data_contrato": None, "agente_gestor": "Manager B", "financiamento": True, "codigo_imovel": "i3"},
        # 4. Started in period (June 5), completed in period (June 20)
        {"transacao_unique_id_pipeimob": "tx4", "data_inicio_venda": "2026-06-05", "data_contrato": "2026-06-20", "agente_gestor": "Manager B", "financiamento": False, "codigo_imovel": "i4"},
        # 5. Excluded data_issue: started in June but negative duration
        {"transacao_unique_id_pipeimob": "tx5", "data_inicio_venda": "2026-06-15", "data_contrato": "2026-06-01", "agente_gestor": "Manager A", "financiamento": True, "codigo_imovel": "i5"}
    ]

    aggregates = compute_contracts_control_data(dataset, "2026-06-01", "2026-06-30", "2026-07-24")

    # Cohort checks
    cohort = aggregates["cohort_summary"]
    # tx3, tx4, tx5 are in cohort (data_inicio_venda in June) -> records_count = 3
    assert cohort["records_count"] == 3
    assert cohort["completed_count"] == 1 # tx4
    assert cohort["in_progress_count"] == 1 # tx3
    assert cohort["data_issue_count"] == 1 # tx5

    # Operations checks
    ops = aggregates["operations_summary"]
    # Opening backlog: tx1, tx2 -> opening_backlog_count = 2
    assert ops["opening_backlog_count"] == 2
    # Started in period (valid): tx3, tx4 -> period_started_count = 2
    assert ops["period_started_count"] == 2
    # Completed in period (valid): tx1, tx2, tx4 -> period_completed_count = 3
    assert ops["period_completed_count"] == 3
    # Ending backlog: tx3 (started June 10, open at June 30) -> ending_backlog_count = 1
    assert ops["ending_backlog_count"] == 1

    # Excluded issue: tx5
    assert ops["excluded_data_issue_count"] == 1

    # Equation validation: ending = opening + started - completed
    assert ops["ending_backlog_count"] == ops["opening_backlog_count"] + ops["period_started_count"] - ops["period_completed_count"]

    # Timeline monthly check (June 2026)
    timeline = aggregates["timeline"]
    assert len(timeline) == 1
    t = timeline[0]
    assert t["month"] == "2026-06"
    assert t["opening_backlog"] == 2
    assert t["started_count"] == 2
    assert t["completed_count"] == 3
    assert t["net_flow"] == -1
    assert t["ending_backlog"] == 1
    assert t["excluded_data_issue_count"] == 1  # tx5 start date is June, so its data issue counts towards June timeline

    # Verify no PII inside aggregates
    payload_str = json.dumps(aggregates, default=str)
    assert "comprador" not in payload_str.lower()
    assert "vendedor" not in payload_str.lower()
    assert "cpf" not in payload_str.lower()

def test_contracts_control_endpoints_summary_and_deals():
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key

    client = TestClient(app)

    # Bypass auth
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}

    from main import contracts_control_cache
    contracts_control_cache.clear()
    try:
        # Mock dataset with duplicates and conflicts
        mock_txs = [
            {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Manager A", "financiamento": True},
            # duplicate with conflict (different manager)
            {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Manager B", "financiamento": True},
            # in progress
            {"transacao_unique_id_pipeimob": "tx2", "codigo_imovel": "456", "data_inicio_venda": "2026-06-15", "data_contrato": None, "agente_gestor": "Manager C", "financiamento": False}
        ]

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            # Test GET /api/contracts-control/summary
            res_sum = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res_sum.status_code == 200
            data_sum = res_sum.json()

            # Extraction quality checks
            eq = data_sum.get("extraction_quality")
            assert eq is not None
            assert eq["raw_records_count"] == 3
            assert eq["unique_records_count"] == 2
            assert eq["duplicate_transaction_count"] == 1
            assert eq["duplicate_conflict_count"] == 1
            assert eq["duplicate_resolution_policy"] == "first_api_occurrence"

            # Cohort summary checks
            cohort = data_sum.get("cohort_summary")
            assert cohort["records_count"] == 2
            assert cohort["completed_count"] == 1
            assert cohort["in_progress_count"] == 1

            # Operations summary checks
            ops = data_sum.get("operations_summary")
            assert ops["opening_backlog_count"] == 0
            assert ops["period_started_count"] == 2
            assert ops["period_completed_count"] == 1
            assert ops["ending_backlog_count"] == 1

            # Timeline check
            tl = data_sum.get("timeline")
            assert len(tl) == 1
            assert tl[0]["provisional"] is True

            # Extraction checks
            ext = data_sum.get("extraction")
            assert ext["coverage_status"] == "unverified"
            assert ext["coverage_start"] == "2020-01-01"

            # Data quality checks
            dq = data_sum.get("data_quality")
            assert dq["scope"] == "cohort"
            assert dq["records_count"] == 2
            assert dq["valid_records_count"] == 2
            assert dq["open_without_contract_date_count"] == 1
            assert dq["mapping_status"]["modality_detail"] == "partial"
            assert dq["mapping_status"]["financing_classification"] == "resolved_api"
            assert dq["mapping_status"]["source_type"] == "resolved_api"
            assert dq["mapping_status"]["responsible"] == "manual_bi"

            # Test GET /api/contracts-control/deals with scope=operations (default)
            res_deals_ops = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30")
            assert res_deals_ops.status_code == 200
            deals_ops = res_deals_ops.json()
            assert deals_ops["total_records"] == 2

            # Verify PII allowlist strictly (exact match)
            allowed_keys = {
                "transaction_id", "property_code", "property_title", "start_date", "contract_date",
                "duration_days", "current_aging_days", "aging_days_at_period_end", "manager",
                "responsible", "modality", "modality_label", "modality_source", "modality_confidence",
                "financing_bank", "financing_amount", "financing_ratio", "modality_flags",
                "source_type", "source_type_label", "current_status", "status_at_period_end",
                "data_quality_flags", "period_roles", "manual_data_version"
            }
            first_deal = deals_ops["deals"][0]
            assert set(first_deal.keys()) == allowed_keys
            assert first_deal["property_title"] is None
            assert first_deal["responsible"] is None
            assert first_deal["source_type"] == "unknown"
            assert "period_roles" in first_deal

            # Test GET /api/contracts-control/deals with scope=cohort
            res_deals_coh = client.get("/api/contracts-control/deals?scope=cohort&start_date=2026-06-01&end_date=2026-06-30")
            assert res_deals_coh.status_code == 200
            deals_coh = res_deals_coh.json()
            assert deals_coh["deals"][0].get("period_roles") is None

            # Test Search query (only manager and property_code allowed)
            res_search_mgr = client.get("/api/contracts-control/deals?search=Manager%20A&start_date=2026-06-01&end_date=2026-06-30")
            assert res_search_mgr.status_code == 200
            search_mgr_deals = res_search_mgr.json()
            assert search_mgr_deals["total_records"] == 1
            assert search_mgr_deals["deals"][0]["manager"] == "Manager A"

    finally:
        app.dependency_overrides.clear()

def test_contracts_control_unauthenticated_requests():
    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    # Endpoint calls without JWT token header must return 401
    res1 = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
    assert res1.status_code == 401
    assert res1.json()["detail"] == "Authentication required."

    res2 = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30")
    assert res2.status_code == 401
    assert res2.json()["detail"] == "Authentication required."

def test_contracts_control_cache_behaviors():
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key, contracts_control_cache

    client = TestClient(app)
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}

    try:
        # Clear cache first to test MISS
        contracts_control_cache.clear()

        mock_txs = [
            {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Manager A", "financiamento": True}
        ]

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)) as mock_fetch, \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            # 1. First request -> MISS
            res1 = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res1.status_code == 200
            assert res1.headers["X-Cache"] == "miss"
            assert mock_fetch.call_count == 1

            # 2. Second request -> HIT (fresh)
            res2 = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res2.status_code == 200
            assert res2.headers["X-Cache"] == "fresh"
            assert mock_fetch.call_count == 1  # no new API fetch

            # 3. Request with refresh=true -> MISS (forces refresh)
            res3 = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30&refresh=true")
            assert res3.status_code == 200
            assert res3.headers["X-Cache"] == "miss"
            assert mock_fetch.call_count == 2

    finally:
        app.dependency_overrides.clear()
        contracts_control_cache.clear()

def test_contracts_control_detailed_boundary_and_edge_cases():
    from main import compute_contracts_control_data, parse_date_to_date_obj, classify_contracts_control_process
    from datetime import date
    import json

    as_of = "2026-07-24"
    as_of_date_obj = date(2026, 7, 24)

    # 1. Status Futuro: start_date > end_date, but <= as_of_date
    # Should not enter cohort, should not enter operational flows, should not be classified as data_issue
    tx_future = {"transacao_unique_id_pipeimob": "tx_fut", "data_inicio_venda": "2026-07-05", "data_contrato": None, "agente_gestor": "Manager A", "financiamento": True}
    res_fut = classify_contracts_control_process(tx_future, as_of_date_obj, date(2026, 6, 30))
    assert res_fut["current_status"] == "in_progress"
    assert res_fut["status_at_period_end"] == "future"  # not data_issue!
    assert "future_start_date" not in res_fut["data_quality_flags"]

    # 2. Boundary: signature exactly on start_date (2026-06-01)
    # 3. Boundary: started before start_date, completed in period
    # 4. Boundary: started in period, completed after end_date (2026-07-15)
    # 5. Boundary: date after as_of_date (future_start_date quality issue)
    # 6. Boundary: data_issue outside timeline distribution (missing start_date)
    # 7. Boundary: identical duplicates & conflicting duplicates
    dataset = [
        # started before (May 10), signed exactly on start_date (June 1) -> opening backlog AND period completed
        {"transacao_unique_id_pipeimob": "tx1", "data_inicio_venda": "2026-05-10", "data_contrato": "2026-06-01", "agente_gestor": "Manager A", "financiamento": True, "codigo_imovel": "i1"},
        # started in period, completed after end_date (July 15) -> started, ending backlog, current=completed, end=in_progress
        {"transacao_unique_id_pipeimob": "tx2", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-07-15", "agente_gestor": "Manager A", "financiamento": True, "codigo_imovel": "i2"},
        # date after as_of_date (July 30) -> data_issue (future start date quality flag)
        {"transacao_unique_id_pipeimob": "tx3", "data_inicio_venda": "2026-07-30", "data_contrato": None, "agente_gestor": "Manager B", "financiamento": False, "codigo_imovel": "i3"},
        # missing start date -> data_issue, not distributed to timeline
        {"transacao_unique_id_pipeimob": "tx4", "data_inicio_venda": "", "data_contrato": "2026-06-15", "agente_gestor": "Manager B", "financiamento": True, "codigo_imovel": "i4"},
        # identical duplicates
        {"transacao_unique_id_pipeimob": "tx5", "data_inicio_venda": "2026-06-05", "data_contrato": "2026-06-20", "agente_gestor": "Manager C", "financiamento": True, "codigo_imovel": "i5"},
        {"transacao_unique_id_pipeimob": "tx5", "data_inicio_venda": "2026-06-05", "data_contrato": "2026-06-20", "agente_gestor": "Manager C", "financiamento": True, "codigo_imovel": "i5"},
        # conflicting duplicates (resolved by first occurrence)
        {"transacao_unique_id_pipeimob": "tx6", "data_inicio_venda": "2026-06-05", "data_contrato": "2026-06-20", "agente_gestor": "Manager X", "financiamento": True, "codigo_imovel": "i6"},
        {"transacao_unique_id_pipeimob": "tx6", "data_inicio_venda": "2026-06-05", "data_contrato": "2026-06-20", "agente_gestor": "Manager Y", "financiamento": True, "codigo_imovel": "i6"}
    ]

    aggregates = compute_contracts_control_data(dataset, "2026-06-01", "2026-06-30", as_of)

    # Verify status at period end vs current status difference for tx2
    tx2_classified = [c for c in aggregates["cohort_txs"] if c["tx"]["transacao_unique_id_pipeimob"] == "tx2"][0]
    assert tx2_classified["current_status"] == "completed"
    assert tx2_classified["status_at_period_end"] == "in_progress"
    assert tx2_classified["aging_days_at_period_end"] == 20  # June 30 - June 10

    # Verify backlog and operational counters
    ops = aggregates["operations_summary"]
    # Opening backlog: tx1
    assert ops["opening_backlog_count"] == 1
    # Period started: tx2, tx5, tx6 (valid started in June) -> 3
    assert ops["period_started_count"] == 3
    # Period completed: tx1, tx5, tx6 (completed in June) -> 3
    assert ops["period_completed_count"] == 3
    # Ending backlog: tx2 (started in June, open at June 30) -> 1
    assert ops["ending_backlog_count"] == 1

    # Equation verification
    assert ops["ending_backlog_count"] == ops["opening_backlog_count"] + ops["period_started_count"] - ops["period_completed_count"]

    # Verify data issues are counted but not in timeline
    assert ops["excluded_data_issue_count"] == 2  # tx3 (future start relative to as_of), tx4 (missing start)
    timeline = aggregates["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["excluded_data_issue_count"] == 0  # not distributed to June timeline because tx3 is in July and tx4 has no start month

    # Verify duplicate counts
    eq = aggregates["extraction_quality"]
    assert eq["duplicate_transaction_count"] == 2
    assert eq["duplicate_conflict_count"] == 1

    # PII verification
    payload_str = json.dumps(aggregates, default=str)
    assert "comprador" not in payload_str.lower()
    assert "vendedor" not in payload_str.lower()
    assert "cpf" not in payload_str.lower()

def test_contracts_control_no_regressions_dashboard_full():
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key

    client = TestClient(app)
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}

    try:
        # Mock simple transactions
        mock_txs = [
            {"total_comissao": 1000.00, "comissionados": [{"comissionado_imobiliaria": True, "comissionado_valor": 1000.00}], "data_pagamento_comissao_prevista": "2026-03-15", "data_assinatura_ccv": "2026-03-15", "valor_contrato": 50000.0, "transacao_unique_id_pipeimob": "tx1", "codigo_contrato": "code1", "agente_gestor": "Manager A"}
        ]

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock", "EXPOSE_RAW_TRANSACTIONS": "false"}):

            # Existing dashboard endpoint must still work fine without regressions
            res = client.get("/api/dashboard/full?data_inicio_ccv=2026-01-01&data_fim_ccv=2026-06-30")
            assert res.status_code == 200
            data = res.json()
            assert data.get("commission_financials") is not None

    finally:
        app.dependency_overrides.clear()

def test_contracts_control_additional_coverage_details():
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key, contracts_control_cache, classify_contracts_control_process, generate_contracts_control_cache_key
    from datetime import date
    import time

    client = TestClient(app)
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}

    try:
        # 1. Test invalid dates and negative duration branches inside classify_contracts_control_process
        as_of = date(2026, 7, 24)
        end_date = date(2026, 6, 30)

        # invalid contract date
        tx_inv_c = {"data_inicio_venda": "2026-06-10", "data_contrato": "invalid-date"}
        res_inv_c = classify_contracts_control_process(tx_inv_c, as_of, end_date)
        assert res_inv_c["status_at_period_end"] == "data_issue"
        assert "invalid_contract_date" in res_inv_c["data_quality_flags"]

        # invalid start date
        tx_inv_s = {"data_inicio_venda": "invalid-date", "data_contrato": "2026-06-15"}
        res_inv_s = classify_contracts_control_process(tx_inv_s, as_of, end_date)
        assert res_inv_s["status_at_period_end"] == "data_issue"
        assert "invalid_start_date" in res_inv_s["data_quality_flags"]

        # 2. Test demo mode data loading
        with patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "demo", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):
            res_demo = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res_demo.status_code == 200
            assert res_demo.headers["X-Data-Mode"] == "demo"

        # 3. Test STALE cache state background refresh task
        contracts_control_cache.clear()
        mock_txs = [
            {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Manager A", "financiamento": True}
        ]

        # set cache stale but not expired
        now = time.time()
        # cached value structure: (val, fresh_until, stale_until)
        # where val is (live_txs, pages_fetched)
        cache_key = generate_contracts_control_cache_key("2020-01-01")
        contracts_control_cache.cache[cache_key] = ((mock_txs, 1), now - 10, now + 1000)

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)) as mock_fetch, \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            # Call should hit stale cache, return stale header, and launch background task
            res_stale = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res_stale.status_code == 200
            assert res_stale.headers["X-Cache"] == "stale"

            # Wait briefly to let background task run
            time.sleep(0.1)

        # 4. Test deals query filters (scope, manager, process_status, modality, aging_bucket, search filtering)
        mock_txs_filters = [
            {"transacao_unique_id_pipeimob": "tx1", "codigo_imovel": "123", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Manager A", "financiamento": True},
            {"transacao_unique_id_pipeimob": "tx2", "codigo_imovel": "456", "data_inicio_venda": "2026-06-28", "data_contrato": None, "agente_gestor": "Manager A", "financiamento": True}
        ]
        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs_filters, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            # Query with non-matching manager
            res_no_match = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&manager=NonExistentManager")
            assert res_no_match.status_code == 200
            assert res_no_match.json()["total_records"] == 0

            # Query with non-matching modality (should hit continue on line 5478)
            res_mod_mismatch = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&modality=deed")
            assert res_mod_mismatch.status_code == 200
            assert res_mod_mismatch.json()["total_records"] == 0

            # Query with non-matching aging_bucket (should hit continue on line 5483)
            res_aging_mismatch = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&aging_bucket=over_30_days")
            assert res_aging_mismatch.status_code == 200
            assert res_aging_mismatch.json()["total_records"] == 0

            # Query with page limit larger than 100 to test caps
            res_cap = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&page_size=101")
            assert res_cap.status_code == 422

    finally:
        app.dependency_overrides.clear()
        contracts_control_cache.clear()

def test_contracts_control_modality_classification_matrix():
    from main import classify_contract_modality, compute_contracts_control_data
    import json

    # 1. financiamento true sem banco e sem parcelas:
    # financing / confirmed / financing_details_incomplete / sem conflict.
    tx1 = {
        "financiamento": True,
        "financiamento_banco": None,
        "forma_pagamento": [],
        "valor_contrato": 200000.00
    }
    res1 = classify_contract_modality(tx1)
    assert res1["modality"] == "financing"
    assert res1["modality_confidence"] == "confirmed"
    assert "financing_details_incomplete" in res1["modality_flags"]
    assert res1["financing_amount"] is None
    assert len([f for f in res1["modality_flags"] if "conflict" in f]) == 0

    # 2. financiamento true com banco:
    # financing / confirmed.
    tx2 = {
        "financiamento": True,
        "financiamento_banco": "Banco Itau",
        "forma_pagamento": [],
        "valor_contrato": 200000.00
    }
    res2 = classify_contract_modality(tx2)
    assert res2["modality"] == "financing"
    assert res2["modality_confidence"] == "confirmed"
    assert res2["financing_bank"] == "Banco Itau"
    assert "financing_details_incomplete" not in res2["modality_flags"]
    assert len([f for f in res2["modality_flags"] if "conflict" in f]) == 0

    # 3. financiamento ausente com banco:
    # financing / inferred.
    tx3 = {
        "financiamento": None,
        "financiamento_banco": "Banco Caixa",
        "forma_pagamento": [],
        "valor_contrato": 200000.00
    }
    res3 = classify_contract_modality(tx3)
    assert res3["modality"] == "financing"
    assert res3["modality_confidence"] == "inferred"
    assert res3["financing_bank"] == "Banco Caixa"
    assert len([f for f in res3["modality_flags"] if "conflict" in f]) == 0

    # 4. banco + parcela de escritura:
    # financing / sem conflict.
    tx4 = {
        "financiamento": None,
        "financiamento_banco": "Banco Caixa",
        "forma_pagamento": [
            {"forma_pagamento_nome": "Parcela Escritura", "forma_pagamento_valor": 50000.00}
        ],
        "valor_contrato": 200000.00
    }
    res4 = classify_contract_modality(tx4)
    assert res4["modality"] == "financing"
    assert res4["modality_confidence"] == "inferred"
    assert len([f for f in res4["modality_flags"] if "conflict" in f]) == 0

    # 5. financiamento false com banco:
    # confidence conflict.
    tx5 = {
        "financiamento": False,
        "financiamento_banco": "Banco Caixa",
        "forma_pagamento": [],
        "valor_contrato": 200000.00
    }
    res5 = classify_contract_modality(tx5)
    assert res5["modality"] == "financing"
    assert res5["modality_confidence"] == "conflict"
    assert "conflict_financing_false_with_bank" in res5["modality_flags"]

    # 6. financiamento false com parcela bancária positiva:
    # confidence conflict.
    tx6 = {
        "financiamento": False,
        "financiamento_banco": None,
        "forma_pagamento": [
            {"forma_pagamento_nome": "credito do financiamento", "forma_pagamento_valor": 120000.00}
        ],
        "valor_contrato": 200000.00
    }
    res6 = classify_contract_modality(tx6)
    assert res6["modality"] == "financing"
    assert res6["modality_confidence"] == "conflict"
    assert "conflict_financing_false_with_payment" in res6["modality_flags"]

    # 7. lançamento sem financiamento:
    # developer_payment / inferred / sem conflict.
    tx7 = {
        "midia_origem_vendedores": "CONSTRUTORA OBRA",
        "financiamento": False,
        "financiamento_banco": None,
        "forma_pagamento": [],
        "valor_contrato": 200000.00
    }
    res7 = classify_contract_modality(tx7)
    assert res7["source_type"] == "launch"
    assert res7["modality"] == "developer_payment"
    assert res7["modality_confidence"] == "inferred"
    assert "launch_without_bank_financing" in res7["modality_flags"]
    assert len([f for f in res7["modality_flags"] if "conflict" in f]) == 0

    # 8. financiamento confirmado sem valor identificável:
    # financing_amount is None / financing_ratio is None.
    tx8 = {
        "financiamento": True,
        "financiamento_banco": "Caixa",
        "forma_pagamento": [],
        "valor_contrato": 200000.00
    }
    res8 = classify_contract_modality(tx8)
    assert res8["modality"] == "financing"
    assert res8["financing_amount"] is None
    assert res8["financing_ratio"] is None

    # 9. valor financiado conhecido:
    # cálculo correto de amount e ratio.
    tx9 = {
        "financiamento": True,
        "forma_pagamento": [{"forma_pagamento_nome": "credito de financiamento", "forma_pagamento_valor": 160000.00}],
        "valor_contrato": 200000.00
    }
    res9 = classify_contract_modality(tx9)
    assert res9["modality"] == "financing"
    assert res9["financing_amount"] == 160000.00
    assert res9["financing_ratio"] == 0.80

    # 10. valor_contrato zero:
    # financing_ratio is None.
    tx10 = {
        "financiamento": True,
        "forma_pagamento": [{"forma_pagamento_nome": "credito de financiamento", "forma_pagamento_valor": 160000.00}],
        "valor_contrato": 0.0
    }
    res10 = classify_contract_modality(tx10)
    assert res10["modality"] == "financing"
    assert res10["financing_amount"] == 160000.00
    assert res10["financing_ratio"] is None

    # 11. agregados:
    # known_count e unknown_count corretos; total ignora valores nulos; média ignora ratios nulos; conflict_count correto.
    mock_dataset = [
        # Deal 1: financing with known amount/ratio (150000, ratio 0.75)
        {
            "transacao_unique_id_pipeimob": "t1",
            "data_inicio_venda": "2026-06-10",
            "data_contrato": "2026-06-25",
            "agente_gestor": "Mgr A",
            "financiamento": True,
            "forma_pagamento": [{"forma_pagamento_nome": "financiamento", "forma_pagamento_valor": 150000.00}],
            "valor_contrato": 200000.00
        },
        # Deal 2: financing with unknown amount/ratio (None, None)
        {
            "transacao_unique_id_pipeimob": "t2",
            "data_inicio_venda": "2026-06-12",
            "data_contrato": "2026-06-28",
            "agente_gestor": "Mgr A",
            "financiamento": True,
            "forma_pagamento": [],
            "valor_contrato": 200000.00
        },
        # Deal 3: financing with conflict (modality classified as financing with confidence conflict)
        {
            "transacao_unique_id_pipeimob": "t3",
            "data_inicio_venda": "2026-06-15",
            "data_contrato": "2026-06-29",
            "agente_gestor": "Mgr A",
            "financiamento": False,
            "financiamento_banco": "Banco Caixa",
            "forma_pagamento": [],
            "valor_contrato": 200000.00
        },
        # Deal 4: deed modality (not financing)
        {
            "transacao_unique_id_pipeimob": "t4",
            "data_inicio_venda": "2026-06-16",
            "data_contrato": "2026-06-30",
            "agente_gestor": "Mgr A",
            "financiamento": False,
            "forma_pagamento": [{"forma_pagamento_nome": "Escritura", "forma_pagamento_valor": 200000.00}],
            "valor_contrato": 200000.00
        }
    ]

    aggregates = compute_contracts_control_data(mock_dataset, "2026-06-01", "2026-06-30", "2026-07-24")
    summary = aggregates["by_modality"]
    assert summary["financing_count"] == 3
    assert summary["financing_amount_known_count"] == 1
    assert summary["financing_amount_unknown_count"] == 2
    assert summary["financing_ratio_known_count"] == 1
    assert summary["financing_total_amount"] == 150000.00
    assert summary["average_financing_ratio"] == 0.75
    assert summary["conflict_count"] == 1
    assert summary["deed_count"] == 1
    assert summary["developer_payment_count"] == 0
    assert summary["unknown_modality_count"] == 0

    # 12. mapping_status final.
    assert aggregates["data_quality"]["mapping_status"] == {
        "property_title": "unresolved",
        "responsible": "manual_bi",
        "financing_classification": "resolved_api",
        "modality_detail": "partial",
        "source_type": "resolved_api",
        "cancellation": "unresolved"
    }

    # 14. nenhuma PII retornada.
    for r in (res1, res2, res3, res4, res5, res6, res7, res8, res9, res10):
        dumped = json.dumps(r)
        assert "comprador" not in dumped.lower()
        assert "vendedor" not in dumped.lower()
        assert "cpf" not in dumped.lower()

# ======================================================================
# FASE 2 — SPRINT 1: TESTES DE PERSISTÊNCIA E OVERLAY
# ======================================================================

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from database import Base
import models.contracts_control
from main import app, get_db_session, verify_backend_api_key
import main
from repositories.contracts_control_repository import ContractsControlRepository
from services.contracts_control_manual_service import ContractsControlManualService

test_db_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "test_temp.db"))
test_engine = create_engine(f"sqlite:///{test_db_file}", connect_args={"check_same_thread": False})

@event.listens_for(test_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s is not None else None)

TestSessionLocal = sessionmaker(bind=test_engine)

@pytest.fixture(name="db_session", scope="function")
def db_session_fixture():
    Base.metadata.create_all(test_engine)
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(test_engine)
        test_engine.dispose()
        if os.path.exists(test_db_file):
            try:
                os.remove(test_db_file)
            except Exception:
                pass

@pytest.fixture(name="client_with_db")
def client_with_db_fixture():
    Base.metadata.create_all(test_engine)
    db = TestSessionLocal()

    def override_get_db_session():
        try:
            yield db
        finally:
            pass

    main.app.dependency_overrides[main.get_db_session] = override_get_db_session
    yield db
    main.app.dependency_overrides.clear()
    db.close()
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()
    if os.path.exists(test_db_file):
        try:
            os.remove(test_db_file)
        except Exception:
            pass

def test_contracts_control_migration_tables_schema(db_session):
    # Verify tables creation
    tables = Base.metadata.tables.keys()
    assert "contracts_control_responsibles" in tables
    assert "contracts_control_manual_data" in tables
    assert "contracts_control_manual_data_history" in tables

def test_contracts_control_migration_downgrade(db_session):
    # Verify we can drop all tables cleanly
    Base.metadata.drop_all(test_engine)
    # Check that they don't exist anymore
    from sqlalchemy import inspect
    inspector = inspect(test_engine)
    assert not inspector.has_table("contracts_control_responsibles")
    assert not inspector.has_table("contracts_control_manual_data")
    assert not inspector.has_table("contracts_control_manual_data_history")

def test_contracts_control_responsible_normalized_name_unique(db_session):
    # Create responsible
    r1 = ContractsControlRepository.create_responsible(db_session, "Sec Vendas")
    db_session.commit()
    assert r1.id is not None
    assert r1.name == "Sec Vendas"
    assert r1.normalized_name == "sec vendas"
    assert r1.active is True

    # Try duplicate normalized name
    with pytest.raises(IntegrityError):
        ContractsControlRepository.create_responsible(db_session, "SEC VENDAS")
        db_session.commit()

def test_contracts_control_responsible_active_inactive(db_session):
    r1 = ContractsControlRepository.create_responsible(db_session, "Sec Ativa", active=True)
    r2 = ContractsControlRepository.create_responsible(db_session, "Sec Inativa", active=False)
    db_session.commit()

    active_list = ContractsControlRepository.list_active_responsibles(db_session, include_inactive=False)
    assert r1 in active_list
    assert r2 not in active_list

    all_list = ContractsControlRepository.list_active_responsibles(db_session, include_inactive=True)
    assert r1 in all_list
    assert r2 in all_list

def test_contracts_control_manual_data_concurrency_locking_and_history(db_session):
    r1 = ContractsControlRepository.create_responsible(db_session, "Sec Vendas")
    db_session.commit()

    # Create manual data attribution
    tx_id = "tx_concurrency_123"
    md = ContractsControlRepository.create_manual_data(db_session, tx_id, r1.id, "sub_user_123")
    db_session.commit()

    assert md.transaction_id == tx_id
    assert md.responsible_id == r1.id
    assert md.version == 1

    # Perform a successful update with expected version
    success = ContractsControlRepository.update_manual_data_optimistic(
        db_session, tx_id, None, expected_version=1, actor_sub="sub_user_456"
    )
    db_session.commit()
    assert success is True

    db_session.refresh(md)
    assert md.responsible_id is None
    assert md.version == 2

    # Perform a failed update with incorrect expected version (Concurrency Conflict)
    success_fail = ContractsControlRepository.update_manual_data_optimistic(
        db_session, tx_id, r1.id, expected_version=1, actor_sub="sub_user_789"
    )
    db_session.commit()
    assert success_fail is False

    # Verify version has not changed
    db_session.refresh(md)
    assert md.version == 2

    # Create history records
    hist = ContractsControlRepository.create_history_record(
        db_session, tx_id, "responsible_id", str(r1.id), None, 1, 2, "sub_user_456"
    )
    db_session.commit()

    assert hist.id is not None
    assert hist.transaction_id == tx_id
    assert hist.previous_value == str(r1.id)
    assert hist.new_value is None
    assert hist.new_version == 2

    history_list = ContractsControlRepository.get_history_by_transaction_id(db_session, tx_id)
    assert hist in history_list

def test_contracts_control_manual_data_rollback_on_error(db_session):
    # Set up to test atomic updates and history registration
    r1 = ContractsControlRepository.create_responsible(db_session, "Sec Vendas")
    db_session.commit()

    tx_id = "tx_rollback_123"
    ContractsControlRepository.create_manual_data(db_session, tx_id, r1.id, "sub_user_123")
    db_session.commit()

    # Simulating update + history write inside a transaction block that fails midway
    try:
        with db_session.begin_nested():
            # Update version from 1 to 2
            success = ContractsControlRepository.update_manual_data_optimistic(
                db_session, tx_id, None, expected_version=1, actor_sub="sub_user_456"
            )
            assert success is True
            # Raise an error to force rollback
            raise ValueError("Forced error to test atomic rollback")
    except ValueError:
        db_session.rollback()

    # Check version was rolled back to 1 and responsible remains r1.id
    md = ContractsControlRepository.get_manual_data_by_transaction_id(db_session, tx_id)
    assert md.version == 1
    assert md.responsible_id == r1.id

def test_contracts_control_deals_endpoint_overlay(client_with_db):
    # Setup database with responsible and manual data
    db = client_with_db
    r1 = ContractsControlRepository.create_responsible(db, "Secretaria Vendas")
    r2 = ContractsControlRepository.create_responsible(db, "Pós-Venda Inativa", active=False)
    db.commit()

    # Associate transaction in mock data to r1 (Secretaria Vendas)
    # the mock deal from test_contracts_control_endpoints_summary_and_deals uses unique ids like 'tx_uniq_1'
    ContractsControlRepository.create_manual_data(db, "tx_uniq_1", r1.id, "sub_user_123")
    ContractsControlRepository.create_manual_data(db, "tx_uniq_2", r2.id, "sub_user_123")
    db.commit()

    # Bypass auth
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}

    from main import contracts_control_cache
    contracts_control_cache.clear()

    try:
        # Mock dataset
        mock_txs = [
            {"transacao_unique_id_pipeimob": "tx_uniq_1", "codigo_imovel": "123", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True},
            {"transacao_unique_id_pipeimob": "tx_uniq_2", "codigo_imovel": "456", "data_inicio_venda": "2026-06-12", "data_contrato": "2026-06-28", "agente_gestor": "Mgr A", "financiamento": True},
            {"transacao_unique_id_pipeimob": "tx_uniq_3", "codigo_imovel": "789", "data_inicio_venda": "2026-06-15", "data_contrato": None, "agente_gestor": "Mgr A", "financiamento": True}
        ]

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            res = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30")
            assert res.status_code == 200
            data = res.json()
            deals = data["deals"]
            assert len(deals) == 3

            # Verify tx_uniq_1 responsible overlay (active responsible)
            deal1 = next(d for d in deals if d["transaction_id"] == "tx_uniq_1")
            assert deal1["responsible"] is not None
            assert deal1["responsible"]["id"] == str(r1.id)
            assert deal1["responsible"]["name"] == "Secretaria Vendas"
            assert deal1["responsible"]["active"] is True

            # Verify tx_uniq_2 responsible overlay (inactive responsible)
            deal2 = next(d for d in deals if d["transaction_id"] == "tx_uniq_2")
            assert deal2["responsible"] is not None
            assert deal2["responsible"]["id"] == str(r2.id)
            assert deal2["responsible"]["name"] == "Pós-Venda Inativa"
            assert deal2["responsible"]["active"] is False

            # Verify tx_uniq_3 responsible overlay (no manual data assigned)
            deal3 = next(d for d in deals if d["transaction_id"] == "tx_uniq_3")
            assert deal3["responsible"] is None

            # Verify responsible filter query param
            res_filtered = client.get(f"/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&responsible={r1.id}")
            assert res_filtered.status_code == 200
            assert res_filtered.json()["total_records"] == 1
            assert res_filtered.json()["deals"][0]["transaction_id"] == "tx_uniq_1"

            # Verify summary manual enrichment indicators
            res_summary = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res_summary.status_code == 200
            summary_data = res_summary.json()
            assert "manual_enrichment" in summary_data

            enrichment = summary_data["manual_enrichment"]
            assert enrichment["status"] == "available"
            assert enrichment["scope"] == "operations"
            assert enrichment["eligible_records_count"] == 3
            assert enrichment["responsible_filled_count"] == 2
            assert enrichment["responsible_pending_count"] == 1
            assert enrichment["responsible_completion_ratio"] == float(2 / 3)
            assert enrichment["last_manual_update_at"] is not None

            # Confirm mapping_status has not altered modality_detail, source_type, etc.
            dq = summary_data["data_quality"]
            assert dq["mapping_status"]["responsible"] == "manual_bi"
            assert dq["mapping_status"]["financing_classification"] == "resolved_api"
            assert dq["mapping_status"]["modality_detail"] == "partial"
            assert dq["mapping_status"]["source_type"] == "resolved_api"

            # Check that no client PII data is returned in deals or summary
            for deal in deals:
                deal_str = json.dumps(deal)
                assert "comprador" not in deal_str.lower()
                assert "vendedor" not in deal_str.lower()
                assert "cpf" not in deal_str.lower()

    finally:
        app.dependency_overrides.clear()
        contracts_control_cache.clear()

def test_contracts_control_deals_endpoint_401_unauthorized():
    # Calling deals without authorization should fail
    from main import verify_backend_api_key
    app.dependency_overrides.clear()

    no_auth_client = TestClient(app)
    response = no_auth_client.get("/api/contracts-control/deals")
    assert response.status_code == 401
    assert "detail" in response.json()

def test_contracts_control_no_write_endpoints_activated():
    import main
    main.CONTRACTS_CONTROL_WRITES_ENABLED = False
    main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}
    try:
        res_patch = client.patch("/api/contracts-control/deals/tx_123/manual-data", json={"responsible_id": None, "version": 1})
        assert res_patch.status_code == 403

        res_post_bulk = client.post("/api/contracts-control/manual-data/bulk", json={"items": [], "responsible_id": None})
        assert res_post_bulk.status_code == 403
    finally:
        main.app.dependency_overrides.clear()

# ======================================================================
# AUDITORIA FINAL — SPRINT 1: NOVOS TESTES DE REQUISITOS
# ======================================================================

def test_contracts_control_deals_pagination_and_responsible_filtering_before_pagination(client_with_db):
    db = client_with_db
    # Create 2 responsibles
    resp_wanted = ContractsControlRepository.create_responsible(db, "Special Secretary")
    resp_other = ContractsControlRepository.create_responsible(db, "Other User")
    db.commit()

    # Assign resp_wanted to deal tx_uniq_5, resp_other to tx_uniq_1
    ContractsControlRepository.create_manual_data(db, "tx_uniq_1", resp_other.id, "actor")
    ContractsControlRepository.create_manual_data(db, "tx_uniq_5", resp_wanted.id, "actor")
    db.commit()

    # Setup auth and cached data bypass
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}
    from main import contracts_control_cache
    contracts_control_cache.clear()

    try:
        # Mock 5 deals
        mock_txs = [
            {"transacao_unique_id_pipeimob": "tx_uniq_1", "codigo_imovel": "1", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True},
            {"transacao_unique_id_pipeimob": "tx_uniq_2", "codigo_imovel": "2", "data_inicio_venda": "2026-06-12", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True},
            {"transacao_unique_id_pipeimob": "tx_uniq_3", "codigo_imovel": "3", "data_inicio_venda": "2026-06-14", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True},
            {"transacao_unique_id_pipeimob": "tx_uniq_4", "codigo_imovel": "4", "data_inicio_venda": "2026-06-16", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True},
            {"transacao_unique_id_pipeimob": "tx_uniq_5", "codigo_imovel": "5", "data_inicio_venda": "2026-06-18", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True}
        ]

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            # Query without filter, check pagination defaults
            res_all = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&page_size=2&page=1")
            assert res_all.status_code == 200
            data_all = res_all.json()
            assert data_all["total_records"] == 5
            assert data_all["total_pages"] == 3
            assert len(data_all["deals"]) == 2

            # Apply filter: special secretary.
            # Filtering happens BEFORE pagination: tx_uniq_5 should be on page 1 of the filtered results.
            res_filtered = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&page_size=2&page=1&responsible=Special Secretary")
            assert res_filtered.status_code == 200
            data_filtered = res_filtered.json()
            assert data_filtered["total_records"] == 1
            assert data_filtered["total_pages"] == 1
            assert len(data_filtered["deals"]) == 1
            assert data_filtered["deals"][0]["transaction_id"] == "tx_uniq_5"

            # Query page out of range (e.g. page=2) -> must return empty list
            res_out_of_range = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30&page_size=2&page=2&responsible=Special Secretary")
            assert res_out_of_range.status_code == 200
            assert res_out_of_range.json()["deals"] == []

    finally:
        app.dependency_overrides.clear()
        contracts_control_cache.clear()

def test_contracts_control_manual_enrichment_scope_and_period_roles_uniqueness(client_with_db):
    db = client_with_db
    resp = ContractsControlRepository.create_responsible(db, "Special Sec")
    db.commit()
    # Associate to tx_uniq_1
    ContractsControlRepository.create_manual_data(db, "tx_uniq_1", resp.id, "actor")
    db.commit()

    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}
    from main import contracts_control_cache
    contracts_control_cache.clear()

    try:
        # tx_uniq_1 started and completed in period -> has 2 roles: started_in_period, completed_in_period.
        # It must not be counted twice in eligible records!
        mock_txs = [
            {"transacao_unique_id_pipeimob": "tx_uniq_1", "codigo_imovel": "1", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True}
        ]

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            res = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res.status_code == 200
            enrichment = res.json()["manual_enrichment"]
            assert enrichment["scope"] == "operations"
            assert enrichment["status"] == "available"
            assert enrichment["eligible_records_count"] == 1
            assert enrichment["responsible_filled_count"] == 1
            assert enrichment["responsible_pending_count"] == 0
            assert enrichment["responsible_completion_ratio"] == 1.0

    finally:
        app.dependency_overrides.clear()
        contracts_control_cache.clear()

def test_contracts_control_database_unavailability_read_endpoints():
    # Force get_db_session to yield None (database unavailable)
    def override_get_db_session():
        yield None

    app.dependency_overrides[main.get_db_session] = override_get_db_session
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "user-123", "role": "authenticated"}
    from main import contracts_control_cache
    contracts_control_cache.clear()

    try:
        mock_txs = [
            {"transacao_unique_id_pipeimob": "tx_uniq_1", "codigo_imovel": "1", "data_inicio_venda": "2026-06-10", "data_contrato": "2026-06-25", "agente_gestor": "Mgr A", "financiamento": True}
        ]

        with patch("main.fetch_all_pipeimob_transactions", return_value=(mock_txs, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            # Deals endpoint must return response, preserving Pipeimob fields, returning responsible = null
            res_deals = client.get("/api/contracts-control/deals?start_date=2026-06-01&end_date=2026-06-30")
            assert res_deals.status_code == 200
            data_deals = res_deals.json()
            assert len(data_deals["deals"]) == 1
            assert data_deals["deals"][0]["transaction_id"] == "tx_uniq_1"
            assert data_deals["deals"][0]["responsible"] is None

            # Summary endpoint must return manual_enrichment.status = "unavailable" and null metrics
            res_sum = client.get("/api/contracts-control/summary?start_date=2026-06-01&end_date=2026-06-30")
            assert res_sum.status_code == 200
            enrichment = res_sum.json()["manual_enrichment"]
            assert enrichment["status"] == "unavailable"
            assert enrichment["scope"] == "operations"
            assert enrichment["eligible_records_count"] is None
            assert enrichment["responsible_filled_count"] is None
            assert enrichment["responsible_pending_count"] is None
            assert enrichment["responsible_completion_ratio"] is None
            assert enrichment["last_manual_update_at"] is None

    finally:
        app.dependency_overrides.clear()
        contracts_control_cache.clear()

def test_contracts_control_name_normalization_semantically_equivalent(db_session):
    from models.contracts_control import normalize_responsible_name

    # Normalization sanity checks
    assert normalize_responsible_name("  José   da  Silva  ") == "jose da silva"
    assert normalize_responsible_name("JOSÉ DA SILVA") == "jose da silva"
    assert normalize_responsible_name("José da Silva") == "jose da silva"

    # Persistence constraint checks: unique normalized name
    r1 = ContractsControlRepository.create_responsible(db_session, "  José   da  Silva  ")
    db_session.commit()
    assert r1.normalized_name == "jose da silva"

    with pytest.raises(IntegrityError):
        # Semantic duplicate: JOSÉ DA SILVA has same normalized name "jose da silva"
        ContractsControlRepository.create_responsible(db_session, "JOSÉ DA SILVA")
        db_session.commit()

# ======================================================================
# AUDITORIA FINAL POSTGRESQL: COBERTURA E INTEGRACAO
# ======================================================================

def test_database_py_comprehensive_coverage():
    import database
    import importlib
    from sqlalchemy.exc import ArgumentError

    # 1. DATABASE_URL ausente / engine não criado na importação
    with patch.dict(os.environ, {}, clear=True):
        importlib.reload(database)
        assert database.engine is None
        assert database.SessionLocal is None

        # 2. get_db raises RuntimeError
        with pytest.raises(RuntimeError) as exc_info:
            next(database.get_db())
        assert "DATABASE_URL is not set" in str(exc_info.value)

    # 3. Dynamic initialization tardia do engine / dynamic init errors
    with pytest.raises(ValueError):
        database.init_db("")

    with pytest.raises(ArgumentError):
        database.init_db("invalid_url://")

    # 4. Successful initialization with SQLite in memory
    database.init_db("sqlite:///:memory:")
    assert database.engine is not None
    assert database.SessionLocal is not None

    from sqlalchemy import event
    @event.listens_for(database.engine, "connect")
    def register_btrim(dbapi_connection, connection_record):
        dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s is not None else None)

    # Base metadata create
    database.Base.metadata.create_all(database.engine)

    # 5. Sessão aberta e fechada corretamente
    with patch("sqlalchemy.orm.Session.close") as mock_close:
        db_gen = database.get_db()
        db_session = next(db_gen)
        assert db_session is not None

        # Check database operations work
        from sqlalchemy import text
        db_session.execute(text("SELECT 1"))

        # 6. Fechamento da sessão em finally/context manager
        try:
            next(db_gen)
        except StopIteration:
            pass
        mock_close.assert_called_once()

    # 7. Rollback em exceção
    db_gen2 = database.get_db()
    db_session2 = next(db_gen2)
    assert db_session2.is_active

    # Simulate exception escaping the yield
    try:
        with patch.object(db_session2, 'rollback') as mock_rollback:
            try:
                db_gen2.throw(ValueError("Simulated route exception"))
            except ValueError:
                pass
            mock_rollback.assert_called_once()
    finally:
        db_session2.close()

    # 8. Dispose do engine nos testes
    database.engine.dispose()

    # 9. Ausência de sessão global compartilhada entre requests
    db_gen_a = database.get_db()
    db_gen_b = database.get_db()
    session_a = next(db_gen_a)
    session_b = next(db_gen_b)
    try:
        assert session_a is not session_b
    finally:
        session_a.close()
        session_b.close()

def test_manual_service_and_repository_edge_cases(db_session):
    # Setup a responsible
    r = ContractsControlRepository.create_responsible(db_session, "Sec Boundary Test")
    db_session.commit()

    # 1. get_responsible_by_id
    r_by_id = ContractsControlRepository.get_responsible_by_id(db_session, r.id)
    assert r_by_id.id == r.id

    # 2. get_responsible_by_normalized_name
    r_by_norm = ContractsControlRepository.get_responsible_by_normalized_name(db_session, "sec boundary test")
    assert r_by_norm.id == r.id

    # 3. get_manual_data_by_transaction_ids empty
    assert ContractsControlRepository.get_manual_data_by_transaction_ids(db_session, []) == []

    # 4. list_responsibles service
    res_list = ContractsControlManualService.list_responsibles(db_session, include_inactive=True)
    assert r in res_list

    # 5. get_manual_data_for_overlay empty service
    assert ContractsControlManualService.get_manual_data_for_overlay(db_session, []) == {}

    # 6. get_enrichment_indicators empty service
    ind = ContractsControlManualService.get_enrichment_indicators(db_session, [])
    assert ind["eligible_records_count"] == 0
    assert ind["responsible_filled_count"] == 0

def test_contracts_control_postgresql_suite():
    # Only run this if a real PostgreSQL DATABASE_URL is available
    pg_url = os.environ.get("DATABASE_URL")
    is_dedicated = os.environ.get("POSTGRES_REQUIRED") == "1" or os.environ.get("POSTGRES_DEDICATED") == "1"
    if not pg_url or not pg_url.startswith("postgresql"):
        if is_dedicated:
            pytest.fail("Dedicated PostgreSQL integration test failed: DATABASE_URL is not set or not a PostgreSQL URL")
        else:
            pytest.skip("Skipping PostgreSQL integration tests because DATABASE_URL is not set or not a PostgreSQL URL")

    # Re-initialize database with the real pg_url
    import database
    database.init_db(pg_url)
    db = database.SessionLocal()

    import uuid
    from models.contracts_control import (
        ContractsControlResponsible,
        ContractsControlManualData,
    )

    # Verify database_dialect is postgresql
    assert database.engine.dialect.name == "postgresql"

    # ======================================================================
    # 2. CICLO REAL DO ALEMBIC NO POSTGRESQL
    # ======================================================================
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config("alembic.ini")

    # 1. Downgrade to base to clean up first
    command.downgrade(alembic_cfg, "base")

    # Check tables do not exist
    from sqlalchemy import inspect
    inspector = inspect(database.engine)
    assert "contracts_control_responsibles" not in inspector.get_table_names()
    assert "contracts_control_manual_data" not in inspector.get_table_names()
    assert "contracts_control_manual_data_history" not in inspector.get_table_names()

    # 2. Upgrade to head
    command.upgrade(alembic_cfg, "head")

    # Check three tables are created
    inspector = inspect(database.engine)
    table_names = inspector.get_table_names()
    assert "contracts_control_responsibles" in table_names
    assert "contracts_control_manual_data" in table_names
    assert "contracts_control_manual_data_history" in table_names

    # ======================================================================
    # 3 & 4. VALIDAR SCHEMA E COMPORTAMENTOS POSTGRESQL
    # ======================================================================
    try:
        # Check active and inactive responsible insertions
        r1 = ContractsControlResponsible(id=uuid.uuid4(), name="Active Responsible", normalized_name="active responsible", active=True)
        r2 = ContractsControlResponsible(id=uuid.uuid4(), name="Inactive Responsible", normalized_name="inactive responsible", active=False)
        db.add_all([r1, r2])
        db.commit()

        # Check unique constraint on normalized_name
        r_dup = ContractsControlResponsible(id=uuid.uuid4(), name="Dup", normalized_name="active responsible")
        db.add(r_dup)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Check chk_responsible_name_not_empty (empty string)
        r_empty_name = ContractsControlResponsible(id=uuid.uuid4(), name="", normalized_name="empty name test")
        db.add(r_empty_name)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Check chk_responsible_name_not_empty (whitespace string with btrim)
        r_space_name = ContractsControlResponsible(id=uuid.uuid4(), name="   ", normalized_name="space name test")
        db.add(r_space_name)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Check chk_responsible_normalized_name_not_empty (empty string)
        r_empty_norm = ContractsControlResponsible(id=uuid.uuid4(), name="Empty Norm Test", normalized_name="")
        db.add(r_empty_norm)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Check chk_responsible_normalized_name_not_empty (whitespace string with btrim)
        r_space_norm = ContractsControlResponsible(id=uuid.uuid4(), name="Space Norm Test", normalized_name="   ")
        db.add(r_space_norm)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Check timezone offsets on dates (TIMESTAMPTZ)
        db.refresh(r1)
        assert r1.created_at.tzinfo is not None

        # Check manual_data creation and foreign key constraint (invalid responsible_id)
        md_invalid_fk = ContractsControlManualData(
            transaction_id="tx_invalid_fk",
            responsible_id=uuid.uuid4(),  # random non-existent uuid
            version=1,
            created_by_sub="actor",
            updated_by_sub="actor"
        )
        db.add(md_invalid_fk)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Check chk_manual_data_version_min (version >= 1 check constraint)
        md_invalid_ver = ContractsControlManualData(
            transaction_id="tx_invalid_ver",
            responsible_id=r1.id,
            version=0,
            created_by_sub="actor",
            updated_by_sub="actor"
        )
        db.add(md_invalid_ver)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Success path creation of manual data
        tx_id = "tx_success_pg"
        md = ContractsControlManualData(
            transaction_id=tx_id,
            responsible_id=r1.id,
            version=1,
            created_by_sub="sub_1",
            updated_by_sub="sub_1"
        )
        db.add(md)
        db.commit()

        # Concurrency: update with correct version
        md.responsible_id = r2.id
        md.version = 2
        md.updated_by_sub = "sub_2"
        db.commit()

        db.refresh(md)
        assert md.responsible_id == r2.id
        assert md.version == 2

        # Concurrency conflict: update with outdated version
        success = ContractsControlRepository.update_manual_data_optimistic(
            db, tx_id, r1.id, expected_version=1, actor_sub="sub_3"
        )
        assert success is False
        db.rollback()

        # History writing in the same transaction block & Rollback conjunto
        try:
            with db.begin_nested():
                success = ContractsControlRepository.update_manual_data_optimistic(
                    db, tx_id, r1.id, expected_version=2, actor_sub="sub_4"
                )
                assert success is True
                # Write history record
                ContractsControlRepository.create_history_record(
                    db, tx_id, "responsible_id", str(r2.id), str(r1.id), 2, 3, "sub_4"
                )
                # Raise exception midway to force rollback
                raise ValueError("Rollback everything")
        except ValueError:
            pass

        # Check version remains 2 and no history record was committed
        db.refresh(md)
        assert md.version == 2
        assert len(ContractsControlRepository.get_history_by_transaction_id(db, tx_id)) == 0

        # ======================================================================
        # 3. CONCORRÊNCIA REAL NO POSTGRESQL
        # ======================================================================
        from sqlalchemy.orm import sessionmaker
        SessionLocal2 = sessionmaker(autocommit=False, autoflush=False, bind=database.engine)
        db1 = database.SessionLocal()
        db2 = SessionLocal2()
        try:
            # First attempt: succeeds and creates version 1
            md1, changed1 = ContractsControlManualService.update_individual_attribution(
                db1, "tx_concurrent_pg", r1.id, 0, "actor_1"
            )
            assert changed1 is True
            assert md1.version == 1
            db1.commit()

            # Second attempt concurrent execution: expects version 0 but fails (unique violation or version conflict)
            # To simulate a true concurrent insert collision before session 2 knows session 1 succeeded,
            # we patch get_manual_data_by_transaction_id to return None, forcing it to attempt the insert on db2.
            from unittest.mock import patch
            with patch("repositories.contracts_control_repository.ContractsControlRepository.get_manual_data_by_transaction_id", return_value=None):
                with pytest.raises(ValueError) as exc_concurrency:
                    ContractsControlManualService.update_individual_attribution(
                        db2, "tx_concurrent_pg", r1.id, 0, "actor_2"
                    )
                assert str(exc_concurrency.value) == "version_conflict"

            # Rollback session 2 and verify it is still usable
            db2.rollback()
            # Try to query something to verify session usability after rollback
            resp_still_usable = db2.get(ContractsControlResponsible, r1.id)
            assert resp_still_usable is not None
            assert resp_still_usable.name == "Active Responsible"
        finally:
            db1.close()
            db2.close()

        # ======================================================================
        # 4. LOTE ATÔMICO REAL NO POSTGRESQL
        # ======================================================================
        # Ensure no records exist for tx_bulk_1 and tx_bulk_2
        # Bulk items: first is valid (version 0), second is invalid (version 999)
        bulk_items = [
            {"transaction_id": "tx_bulk_1", "version": 0},
            {"transaction_id": "tx_bulk_2", "version": 999}
        ]
        with pytest.raises(ValueError) as exc_bulk:
            ContractsControlManualService.update_bulk_attribution(
                db, bulk_items, r1.id, "actor_bulk", {"tx_bulk_1", "tx_bulk_2"}
            )
        assert str(exc_bulk.value) == "version_conflict"

        # Confirm after error: neither is persisted/altered, versions not incremented, no history
        from models.contracts_control import ContractsControlManualData
        md_b1 = db.get(ContractsControlManualData, "tx_bulk_1")
        md_b2 = db.get(ContractsControlManualData, "tx_bulk_2")
        assert md_b1 is None
        assert md_b2 is None

        # Check history tables are empty for these transaction IDs
        assert len(ContractsControlRepository.get_history_by_transaction_id(db, "tx_bulk_1")) == 0
        assert len(ContractsControlRepository.get_history_by_transaction_id(db, "tx_bulk_2")) == 0

        # ======================================================================
        # 5. IDEMPOTÊNCIA NO POSTGRESQL
        # ======================================================================
        # A. responsible_id null + version 0 (Case A)
        md_a, changed_a = ContractsControlManualService.update_individual_attribution(
            db, "tx_idem_a", None, 0, "actor"
        )
        assert changed_a is False
        assert md_a.version == 0
        assert md_a.responsible_id is None
        # Check database: no record exists
        assert db.get(ContractsControlManualData, "tx_idem_a") is None
        assert len(ContractsControlRepository.get_history_by_transaction_id(db, "tx_idem_a")) == 0

        # Create record for Case B/C
        md_init, changed_init = ContractsControlManualService.update_individual_attribution(
            db, "tx_idem_b", r1.id, 0, "actor"
        )
        assert changed_init is True
        assert md_init.version == 1
        db.commit()

        # B. mesmo responsável já atribuído (Case B)
        md_b, changed_b = ContractsControlManualService.update_individual_attribution(
            db, "tx_idem_b", r1.id, 1, "actor"
        )
        assert changed_b is False
        assert md_b.version == 1
        # Check no additional history record was written
        assert len(ContractsControlRepository.get_history_by_transaction_id(db, "tx_idem_b")) == 1

        # Create another active responsible for update
        r_active = ContractsControlResponsible(id=uuid.uuid4(), name="Another Active Resp", normalized_name="another active resp", active=True)
        db.add(r_active)
        db.commit()

        # C. alteração efetiva (Case C)
        md_c, changed_c = ContractsControlManualService.update_individual_attribution(
            db, "tx_idem_b", r_active.id, 1, "actor_c"
        )
        assert changed_c is True
        assert md_c.version == 2
        db.commit()
        # Check history record created
        hist = ContractsControlRepository.get_history_by_transaction_id(db, "tx_idem_b")
        assert len(hist) == 2
        update_record = next(h for h in hist if h.previous_value is not None)
        assert update_record.field_name == "responsible_id"
        assert update_record.previous_value == str(r1.id)
        assert update_record.new_value == str(r_active.id)

        # ======================================================================
        # 6. VALIDAR UNIVERSO PIPEIMOB
        # ======================================================================
        from fastapi.testclient import TestClient
        import main
        client = TestClient(main.app)

        # Mock dependency overrides for temporary admin credentials
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
        }
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"

        try:
            # Mock load_contracts_control_dataset to raise error -> 503
            async def mock_load_error(*args, **kwargs):
                raise Exception("Pipeimob api timeout")

            with patch("main.load_contracts_control_dataset", side_effect=mock_load_error):
                res_err = client.patch(
                    "/api/contracts-control/deals/tx_any/manual-data",
                    json={"responsible_id": str(r1.id), "version": 0}
                )
                assert res_err.status_code == 503

            # Mock load_contracts_control_dataset to return a dataset -> non-existent ID -> 404
            mock_universe = [
                {"tx": {"transacao_unique_id_pipeimob": f"tx_page_{i}"}} for i in range(150)
            ]
            async def mock_load_universe(*args, **kwargs):
                return "demo", "mock", mock_universe, 1, "stale"

            with patch("main.load_contracts_control_dataset", side_effect=mock_load_universe):
                # non-existent ID -> 404
                res_not_found = client.patch(
                    "/api/contracts-control/deals/tx_non_existent/manual-data",
                    json={"responsible_id": str(r1.id), "version": 0}
                )
                assert res_not_found.status_code == 404

                # exists but on page > 1 (e.g. index 120) -> 200 (since universe complete/deduplicated is checked)
                res_page_2 = client.patch(
                    f"/api/contracts-control/deals/tx_page_120/manual-data",
                    json={"responsible_id": str(r1.id), "version": 0}
                )
                assert res_page_2.status_code == 200

                # Flat/raw transaction dict (without nested "tx" key) -> should also be parsed and return 200/404 correctly
                mock_flat_universe = [
                    {"transacao_unique_id_pipeimob": f"tx_flat_{i}"} for i in range(50)
                ]
                async def mock_load_flat_universe(*args, **kwargs):
                    return "demo", "mock", mock_flat_universe, 1, "stale"

                with patch("main.load_contracts_control_dataset", side_effect=mock_load_flat_universe):
                    res_flat_200 = client.patch(
                        "/api/contracts-control/deals/tx_flat_10/manual-data",
                        json={"responsible_id": str(r1.id), "version": 0}
                    )
                    assert res_flat_200.status_code == 200
        finally:
            main.app.dependency_overrides.clear()
            os.environ.pop("CONTRACTS_CONTROL_WRITES_ENABLED", None)
            os.environ.pop("CONTRACTS_CONTROL_ADMIN_SUBS", None)

    finally:
        db.close()
        # Clean up database tables in downgrade / upgrade cycle
        command.downgrade(alembic_cfg, "base")
        command.upgrade(alembic_cfg, "head")

def test_get_contracts_control_dataset_for_write_cache_miss():
    from main import get_contracts_control_dataset_for_write, contracts_control_cache
    from fastapi import HTTPException
    import pytest
    import sys

    contracts_control_cache.clear()

    with patch("main.get_current_data_mode_and_connection", return_value=("live", "ok")):
        # Mock sys.modules to simulate non-pytest runtime environment
        real_modules = sys.modules.copy()
        if "pytest" in real_modules:
            del real_modules["pytest"]

        with patch("sys.modules", real_modules):
            with pytest.raises(HTTPException) as exc:
                import asyncio
                asyncio.run(get_contracts_control_dataset_for_write())
            assert exc.value.status_code == 503
            assert "cache is empty" in exc.value.detail

def test_contracts_control_patch_stage_logging(client_with_db):
    from main import contracts_control_cache, generate_contracts_control_cache_key
    import main
    import logging
    from unittest.mock import MagicMock

    main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
        "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
    }
    os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
    os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"

    try:
        # Populate cache
        cache_key = generate_contracts_control_cache_key("2020-01-01")
        contracts_control_cache.set(cache_key, ([], 1))

        # Capture logs
        logger = logging.getLogger("main")
        mock_logger = MagicMock()
        logger.info = mock_logger.info
        logger.error = mock_logger.error

        # This will fail with 404 (since tx is not found in empty cache), but it should log load_dataset start/error or end
        res = client.patch(
            "/api/contracts-control/deals/tx_not_in_cache/manual-data",
            json={"responsible_id": None, "version": 0}
        )
        assert res.status_code == 404

        # Check logs
        start_calls = [call for call in mock_logger.info.call_args_list if "CC_PATCH_STAGE_START" in call[0][0]]
        assert len(start_calls) >= 1
        assert "stage=load_dataset" in start_calls[0][0][0]
    finally:
        main.app.dependency_overrides.clear()
        os.environ.pop("CONTRACTS_CONTROL_WRITES_ENABLED", None)
        os.environ.pop("CONTRACTS_CONTROL_ADMIN_SUBS", None)

def test_contracts_control_read_endpoints_overlay_integration(client_with_db):
    db = client_with_db
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    from main import contracts_control_cache, generate_contracts_control_cache_key
    from repositories.contracts_control_repository import ContractsControlRepository
    from models.contracts_control import normalize_responsible_name
    from main import HistoryRecordItem, ContractsControlResponsibleReference

    # 1. Create active responsible
    r = ContractsControlRepository.create_responsible(db, "Ana Cristina", active=True)

    # 2. Create manual record with transaction_id as integer in simulated dataset
    tx_numeric = 19382103
    tx_numeric_str = str(tx_numeric)

    # Create manual data record
    ContractsControlRepository.create_manual_data(db, tx_numeric_str, r.id, "admin_sub_1")
    # Create history record
    ContractsControlRepository.create_history_record(
        db, tx_numeric_str, "responsible_id", None, str(r.id), 0, 1, "admin_sub_1"
    )
    db.commit()

    # 3. Simulate dataset containing the numeric transaction_id (dataset flat)
    simulated_flat_dataset = [
        {
            "transacao_unique_id_pipeimob": tx_numeric,
            "data_inicio_venda": "2026-01-10",
            "agente_gestor": "Gestor Ana",
            "agente_gestor_grupo_filial": "Filial A",
            "agente_gestor_grupos_a_que_pertence": ["group_1"]
        }
    ]

    # Mock dependency verification
    main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
        "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
    }
    os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
    os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"

    try:
        # Mock load_contracts_control_dataset to return the simulated flat dataset
        async def mock_load_simulated(*args, **kwargs):
            return "demo", "synthetic_mock", simulated_flat_dataset, 1, "stale"

        with patch("main.load_contracts_control_dataset", side_effect=mock_load_simulated):
            # 6. Call GET /deals
            res_deals = client.get("/api/contracts-control/deals?scope=operations")
            assert res_deals.status_code == 200
            deals_data = res_deals.json()["deals"]
            assert len(deals_data) == 1
            deal = deals_data[0]
            assert deal["transaction_id"] == tx_numeric_str
            assert deal["responsible"]["name"] == "Ana Cristina"
            assert deal["manual_data_version"] == 1

            # 7. Call GET /summary
            res_summary = client.get("/api/contracts-control/summary")
            assert res_summary.status_code == 200
            summary_data = res_summary.json()["manual_enrichment"]
            assert summary_data["eligible_records_count"] == 1
            assert summary_data["responsible_filled_count"] == 1
            assert summary_data["responsible_pending_count"] == 0
            assert abs(summary_data["responsible_completion_ratio"] - 1.0) < 1e-5

            # 8. Call GET /history
            res_history = client.get(f"/api/contracts-control/deals/{tx_numeric}/manual-data/history")
            assert res_history.status_code == 200
            history_data = res_history.json()
            assert len(history_data) == 1
            hist_event = history_data[0]
            assert hist_event["field_name"] == "responsible_id"
            assert hist_event["previous_responsible"] is None
            assert hist_event["new_responsible"]["current_name"] == "Ana Cristina"
            assert hist_event["new_responsible"]["active"] is True

            # 9. Test variations: transaction_id longo e alfanumérico, dataset aninhado, etc.
            tx_long_alpha = "tx_long_alpha_12345_abc"
            ContractsControlRepository.create_manual_data(db, tx_long_alpha, r.id, "admin_sub_1")
            ContractsControlRepository.create_history_record(
                db, tx_long_alpha, "responsible_id", None, str(r.id), 0, 1, "admin_sub_1"
            )
            db.commit()

            simulated_nested_dataset = [
                {
                    "tx": {
                        "transacao_unique_id_pipeimob": tx_long_alpha,
                        "data_inicio_venda": "2026-01-10",
                        "agente_gestor": "Gestor Ana",
                        "agente_gestor_grupo_filial": "Filial A",
                        "agente_gestor_grupos_a_que_pertence": ["group_1"]
                    }
                }
            ]

            async def mock_load_nested(*args, **kwargs):
                return "demo", "synthetic_mock", simulated_nested_dataset, 1, "stale"

            with patch("main.load_contracts_control_dataset", side_effect=mock_load_nested):
                # Call PATCH with nested dataset mock to verify compatibility
                res_patch_nested = client.patch(
                    f"/api/contracts-control/deals/{tx_long_alpha}/manual-data",
                    json={"responsible_id": None, "version": 1}
                )
                assert res_patch_nested.status_code == 200

                # Call GET /history for long alpha
                res_hist_nested = client.get(f"/api/contracts-control/deals/{tx_long_alpha}/manual-data/history")
                assert res_hist_nested.status_code == 200
                assert len(res_hist_nested.json()) == 2

            # Responsible subsequently inactive
            ContractsControlRepository.update_responsible(db, r.id, active=False)
            db.commit()

            async def mock_load_simulated2(*args, **kwargs):
                return "demo", "synthetic_mock", simulated_flat_dataset, 1, "stale"

            with patch("main.load_contracts_control_dataset", side_effect=mock_load_simulated2):
                res_deals_inactive = client.get("/api/contracts-control/deals?scope=operations")
                assert res_deals_inactive.status_code == 200
                deal_inactive = res_deals_inactive.json()["deals"][0]
                assert deal_inactive["responsible"]["active"] is False

                res_history_inactive = client.get(f"/api/contracts-control/deals/{tx_numeric}/manual-data/history")
                assert res_history_inactive.status_code == 200
                assert res_history_inactive.json()[0]["new_responsible"]["active"] is False
    finally:
        main.app.dependency_overrides.clear()
        os.environ.pop("CONTRACTS_CONTROL_WRITES_ENABLED", None)
        os.environ.pop("CONTRACTS_CONTROL_ADMIN_SUBS", None)

def test_contracts_control_responsible_filter_regression(client_with_db):
    db = client_with_db
    from fastapi.testclient import TestClient
    import main
    from main import contracts_control_cache
    from repositories.contracts_control_repository import ContractsControlRepository

    client = TestClient(main.app)

    # 1. Create active responsible Ana Cristina
    r_ana = ContractsControlRepository.create_responsible(db, "Ana Cristina", active=True)
    # 2. Create inactive responsible
    r_inactive = ContractsControlRepository.create_responsible(db, "Inativo Carlos", active=False)

    tx_ana = "tx_ana_999"
    tx_unassigned = "tx_unassigned_888"
    tx_inactive = "tx_inactive_777"

    # Create manual data and history
    ContractsControlRepository.create_manual_data(db, tx_ana, r_ana.id, "admin_sub_1")
    ContractsControlRepository.create_manual_data(db, tx_inactive, r_inactive.id, "admin_sub_1")
    db.commit()

    simulated_dataset = [
        {
            "transacao_unique_id_pipeimob": tx_ana,
            "data_inicio_venda": "2026-01-10",
            "agente_gestor": "Gestor Ana"
        },
        {
            "transacao_unique_id_pipeimob": tx_unassigned,
            "data_inicio_venda": "2026-01-11",
            "agente_gestor": "Gestor B"
        },
        {
            "transacao_unique_id_pipeimob": tx_inactive,
            "data_inicio_venda": "2026-01-12",
            "agente_gestor": "Gestor C"
        }
    ]

    main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
        "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
    }
    os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
    os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"

    try:
        async def mock_load(*args, **kwargs):
            return "demo", "synthetic_mock", simulated_dataset, 1, "stale"

        with patch("main.load_contracts_control_dataset", side_effect=mock_load):
            # Clear caches before test starts
            contracts_control_cache.clear()

            # --- Cenário A: cache global aquecido ---
            # 1. GET /deals sem responsavel (global)
            res_global_1 = client.get("/api/contracts-control/deals?scope=operations")
            assert res_global_1.status_code == 200
            assert res_global_1.headers.get("X-Cache") in ("stale", "miss", "fresh")
            data_global_1 = res_global_1.json()
            assert data_global_1["total_records"] == 3

            # 2. GET /deals com responsavel=Ana Cristina (filtrado)
            res_filtered_1 = client.get("/api/contracts-control/deals?scope=operations&responsavel=Ana+Cristina")
            assert res_filtered_1.status_code == 200
            data_filtered_1 = res_filtered_1.json()
            assert data_filtered_1["total_records"] == 1
            assert data_filtered_1["deals"][0]["transaction_id"] == tx_ana
            assert data_filtered_1["deals"][0]["responsible"]["name"] == "Ana Cristina"

            # --- Cenário B: ordem inversa ---
            # Clear caches
            contracts_control_cache.clear()
            # 1. GET filtrado primeiro
            res_filtered_2 = client.get("/api/contracts-control/deals?scope=operations&responsavel=Ana+Cristina")
            assert res_filtered_2.status_code == 200
            assert res_filtered_2.json()["total_records"] == 1
            # 2. GET global segundo
            res_global_2 = client.get("/api/contracts-control/deals?scope=operations")
            assert res_global_2.status_code == 200
            assert res_global_2.json()["total_records"] == 3

            # --- Cenário C: paginação ---
            # filtro com um resultado; page=1; page_size=25; total_records=1; total_pages=1
            res_page = client.get("/api/contracts-control/deals?scope=operations&responsavel=Ana+Cristina&page=1&page_size=25")
            assert res_page.status_code == 200
            data_page = res_page.json()
            assert data_page["page"] == 1
            assert data_page["page_size"] == 25
            assert data_page["total_records"] == 1
            assert data_page["total_pages"] == 1
            assert len(data_page["deals"]) == 1

            # --- Cenário D: summary ---
            # elegíveis=1; preenchidos=1; pendentes=0; ratio=1.0
            res_sum = client.get("/api/contracts-control/summary?responsavel=Ana+Cristina")
            assert res_sum.status_code == 200
            sum_data = res_sum.json()["manual_enrichment"]
            assert sum_data["eligible_records_count"] == 1
            assert sum_data["responsible_filled_count"] == 1
            assert sum_data["responsible_pending_count"] == 0
            assert abs(sum_data["responsible_completion_ratio"] - 1.0) < 1e-5

            # --- Cenário E: responsável inexistente ---
            # deals vazio; total_records=0; summary sem registros
            res_nonexistent = client.get("/api/contracts-control/deals?scope=operations&responsavel=Nonexistent")
            assert res_nonexistent.status_code == 200
            data_nonexistent = res_nonexistent.json()
            assert data_nonexistent["total_records"] == 0
            assert data_nonexistent["total_pages"] == 1
            assert len(data_nonexistent["deals"]) == 0

            res_sum_nonexistent = client.get("/api/contracts-control/summary?responsavel=Nonexistent")
            assert res_sum_nonexistent.status_code == 200
            sum_nonexistent_data = res_sum_nonexistent.json()["manual_enrichment"]
            assert sum_nonexistent_data["eligible_records_count"] == 0
            assert sum_nonexistent_data["responsible_filled_count"] == 0
            assert sum_nonexistent_data["responsible_pending_count"] == 0
            assert sum_nonexistent_data["responsible_completion_ratio"] == 0.0

            # --- Cenário F: responsável inativo ---
            # processo já atribuído continua localizável pelo nome; active=false é preservado na resposta
            res_inactive = client.get("/api/contracts-control/deals?scope=operations&responsavel=Inativo+Carlos")
            assert res_inactive.status_code == 200
            data_inactive = res_inactive.json()
            assert data_inactive["total_records"] == 1
            assert data_inactive["deals"][0]["transaction_id"] == tx_inactive
            assert data_inactive["deals"][0]["responsible"]["active"] is False

            # --- Cenário G: transaction_id longo e alfanumérico ---
            # (provido na configuração acima, tx_ana e tx_inactive são alfanuméricos e longos)

            # --- Cenário H: cache e parâmetros ---
            # As chaves são diferentes, então o cabeçalho X-Cache deve se comportar de forma isolada
            contracts_control_cache.clear()
            # 1. Primeira chamada deals para Ana Cristina -> Miss
            r1 = client.get("/api/contracts-control/deals?scope=operations&responsavel=Ana+Cristina")
            assert r1.headers.get("X-Cache") in ("stale", "miss")
            # 2. Segunda chamada deals para Ana Cristina -> Fresh/Stale (Hit)
            r2 = client.get("/api/contracts-control/deals?scope=operations&responsavel=Ana+Cristina")
            assert r2.headers.get("X-Cache") in ("fresh", "stale")
            # 3. Chamada deals para Carlos -> Miss (pois a chave é diferente!)
            r3 = client.get("/api/contracts-control/deals?scope=operations&responsavel=Inativo+Carlos")
            assert r3.headers.get("X-Cache") in ("stale", "miss")

            # --- Teste de Invalidação no PATCH ---
            # 1. Aquece o cache do GET deals Ana Cristina
            client.get("/api/contracts-control/deals?scope=operations&responsavel=Ana+Cristina")
            # 2. Executa um PATCH de atribuição
            res_patch = client.patch(
                f"/api/contracts-control/deals/{tx_ana}/manual-data",
                json={"responsible_id": str(r_ana.id), "version": 1}
            )
            assert res_patch.status_code == 200
            # 3. A leitura seguinte para Ana Cristina deve ser Miss/Stale (não fresh) devido ao cache invalidado!
            r_post_patch = client.get("/api/contracts-control/deals?scope=operations&responsavel=Ana+Cristina")
            assert r_post_patch.headers.get("X-Cache") in ("stale", "miss")

    finally:
        main.app.dependency_overrides.clear()
        os.environ.pop("CONTRACTS_CONTROL_WRITES_ENABLED", None)
        os.environ.pop("CONTRACTS_CONTROL_ADMIN_SUBS", None)

def test_bi_secretaria_non_interference(client_with_db):
    db = client_with_db
    from fastapi.testclient import TestClient
    import main
    from main import contracts_control_cache, dashboard_cache, generate_dashboard_cache_key
    from repositories.contracts_control_repository import ContractsControlRepository
    from zoneinfo import ZoneInfo

    client = TestClient(main.app)

    # Clean all caches
    contracts_control_cache.clear()
    dashboard_cache.clear()

    # Create mock CRM dataset used by BI and CC
    mock_crm_dataset = [
        {
            "transacao_unique_id_pipeimob": "tx_non_int_1",
            "codigo_imovel": "IMOB_101",
            "data_inicio_venda": "2026-06-10",
            "data_contrato": "2026-06-25",
            "agente_gestor": "Manager A",
            "financiamento": True,
            "valor_gralha_vgc": 1000.0,
            "data_inicio_criacao": "2026-06-01",
            "data_inicio_ccv": "2026-06-01",
            "etapa_nome": "Vendido",
            "origem_lead": "Portal"
        }
    ]

    # Setup mocks
    main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
        "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
    }
    os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
    os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"

    # Mock fetch for both BI and CC raw loaders
    async def mock_load_cc(*args, **kwargs):
        return "demo", "synthetic_mock", mock_crm_dataset, 1, "miss"

    try:
        with patch("main.load_contracts_control_dataset", side_effect=mock_load_cc), \
             patch("main.fetch_all_pipeimob_transactions", return_value=(mock_crm_dataset, 1)), \
             patch.dict(os.environ, {"PIPEIMOB_DATA_MODE": "live", "PIPEIMOB_API_KEY": "mock", "PIPEIMOB_SECRET_KEY": "mock"}):

            # Clear cache first
            contracts_control_cache.clear()
            dashboard_cache.clear()

            # --- PRE-TEST: Get initial BI payload ---
            res_bi_initial = client.get("/api/dashboard/full?data_inicio_criacao=2026-06-01&data_fim_criacao=2026-06-30")
            assert res_bi_initial.status_code == 200
            bi_initial_data = res_bi_initial.json()

            def clean_dynamic_fields(d):
                import copy
                c = copy.deepcopy(d)
                if "generated_at" in c:
                    c["generated_at"] = "STATIC"
                if "data_quality" in c and "generated_at" in c["data_quality"]:
                    c["data_quality"]["generated_at"] = "STATIC"
                return c

            # --- A. Atribuir responsável na Secretaria ---
            # dados da Secretaria mudam; payload e indicadores do BI permanecem idênticos.
            r_ana = ContractsControlRepository.create_responsible(db, "Ana Cristina", active=True)
            db.commit()

            # Assign responsible via Secretaria endpoint
            res_patch = client.patch(
                "/api/contracts-control/deals/tx_non_int_1/manual-data",
                json={"responsible_id": str(r_ana.id), "version": 0}
            )
            assert res_patch.status_code == 200

            # Verify Secretaria (Secretaria data has changed)
            res_deals_after_assign = client.get("/api/contracts-control/deals?scope=operations")
            assert res_deals_after_assign.status_code == 200
            assert res_deals_after_assign.json()["deals"][0]["responsible"]["name"] == "Ana Cristina"

            # Verify BI (BI payload and indicators remain identical)
            res_bi_after_assign = client.get("/api/dashboard/full?data_inicio_criacao=2026-06-01&data_fim_criacao=2026-06-30")
            assert res_bi_after_assign.status_code == 200
            assert clean_dynamic_fields(res_bi_after_assign.json()) == clean_dynamic_fields(bi_initial_data)

            # --- B. Limpar responsável na Secretaria ---
            # histórico e versão da Secretaria mudam; BI permanece inalterado.
            # Clean responsible by setting to null
            res_patch_clear = client.patch(
                "/api/contracts-control/deals/tx_non_int_1/manual-data",
                json={"responsible_id": None, "version": 1}
            )
            assert res_patch_clear.status_code == 200

            # Verify Secretaria (responsible is now null, version is 2)
            res_deals_after_clear = client.get("/api/contracts-control/deals?scope=operations")
            assert res_deals_after_clear.status_code == 200
            assert res_deals_after_clear.json()["deals"][0]["responsible"] is None
            assert res_deals_after_clear.json()["deals"][0]["manual_data_version"] == 2

            # Verify BI remains completely unaltered
            res_bi_after_clear = client.get("/api/dashboard/full?data_inicio_criacao=2026-06-01&data_fim_criacao=2026-06-30")
            assert res_bi_after_clear.status_code == 200
            assert clean_dynamic_fields(res_bi_after_clear.json()) == clean_dynamic_fields(bi_initial_data)

            # --- C. Atualizar/recalcular BI ---
            # responsáveis manuais da Secretaria permanecem intactos.
            # Reassign responsible in CC first
            res_reassign = client.patch(
                "/api/contracts-control/deals/tx_non_int_1/manual-data",
                json={"responsible_id": str(r_ana.id), "version": 2}
            )
            assert res_reassign.status_code == 200

            # Trigger BI recalculation (endpoint call with refresh=True)
            res_bi_recalc = client.get("/api/dashboard/full?data_inicio_criacao=2026-06-01&data_fim_criacao=2026-06-30&refresh=True")
            assert res_bi_recalc.status_code == 200

            # Verify CC responsible remains intact
            res_deals_after_bi_recalc = client.get("/api/contracts-control/deals?scope=operations")
            assert res_deals_after_bi_recalc.json()["deals"][0]["responsible"]["name"] == "Ana Cristina"

            # --- D. Invalidar cache da Secretaria ---
            # cache do BI permanece válido.
            # Populate BI cache
            client.get("/api/dashboard/full?data_inicio_criacao=2026-06-01&data_fim_criacao=2026-06-30")
            bi_cache_key = generate_dashboard_cache_key(data_inicio_criacao="2026-06-01", data_fim_criacao="2026-06-30")
            assert dashboard_cache.get(bi_cache_key) is not None

            # Invalidate CC cache
            contracts_control_cache.clear_endpoint_caches()

            # Verify BI cache remains valid
            assert dashboard_cache.get(bi_cache_key) is not None

            # --- E. Invalidar cache do BI ---
            # cache e dados manuais da Secretaria permanecem válidos.
            # Populate CC cache
            client.get("/api/contracts-control/deals?scope=operations")
            cc_deals_key = (
                "contracts-control",
                "deals",
                main.CONTRACTS_CONTROL_CACHE_VERSION,
                "2026-01-01",
                main.datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d"),
                "operations",
                None, None, None, None, None, None, None,
                1, 25
            )
            assert contracts_control_cache.get(cc_deals_key) is not None

            # Invalidate BI cache
            dashboard_cache.clear()

            # Verify CC cache and manual database data remain completely valid
            assert contracts_control_cache.get(cc_deals_key) is not None
            res_deals_final = client.get("/api/contracts-control/deals?scope=operations")
            assert res_deals_final.json()["deals"][0]["responsible"]["name"] == "Ana Cristina"

            # --- F. Importar planilha ---
            # gravações ocorrem somente nas tabelas da Secretaria; nenhuma tabela ou projeção do BI é modificada.
            # Simulation of spreadsheet import function following requirements:
            # planilha.property_code -> index over pipeimob negocios -> transaction_id -> Secretaria manual responsible.

            # 1. Spreadsheet row simulated: Property IMOB_101 belongs to responsible Ana Cristina
            row = {"property_code": "IMOB_101", "responsible_name": "Ana Cristina"}

            # 2. Get the index on property code from the raw pipeimob dataset (simulating no BI query or dataframe usage)
            prop_index = {tx.get("codigo_imovel"): tx.get("transacao_unique_id_pipeimob") for tx in mock_crm_dataset}
            matched_tx_id = prop_index.get(row["property_code"])
            assert matched_tx_id == "tx_non_int_1"

            # 3. Write only to Secretaria's manual table
            # Verify no BI tables or dashboard_cache are modified
            dashboard_cache.clear()

            md_record = ContractsControlRepository.get_manual_data_by_transaction_id(db, matched_tx_id)
            if md_record:
                initial_version = md_record.version
                ContractsControlRepository.update_manual_data_optimistic(
                    db, matched_tx_id, r_ana.id, initial_version, "importer_sub"
                )
                db.commit()
                db.refresh(md_record)
                updated_md = md_record
            else:
                initial_version = 0
                updated_md = ContractsControlRepository.create_manual_data(
                    db, matched_tx_id, r_ana.id, "importer_sub"
                )
                db.commit()

            assert updated_md.version == initial_version + 1
            assert updated_md.responsible_id == r_ana.id

            # Assert BI cache or database is not affected
            res_bi_post_import = client.get("/api/dashboard/full?data_inicio_criacao=2026-06-01&data_fim_criacao=2026-06-30")
            assert res_bi_post_import.status_code == 200
            assert clean_dynamic_fields(res_bi_post_import.json()) == clean_dynamic_fields(bi_initial_data)

    finally:
        main.app.dependency_overrides.clear()
        os.environ.pop("CONTRACTS_CONTROL_WRITES_ENABLED", None)
        os.environ.pop("CONTRACTS_CONTROL_ADMIN_SUBS", None)

def test_verify_backend_api_key_invalid_format():
    from main import verify_backend_api_key, AuthException
    import pytest
    import asyncio
    with pytest.raises(AuthException) as exc:
        asyncio.run(verify_backend_api_key(authorization="Basic 12345"))
    assert exc.value.status_code == 401

def test_main_http_timeout_value_error():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with patch.dict(os.environ, {"PIPEIMOB_HTTP_TIMEOUT_SECONDS": "invalid"}):
            import main
            import importlib
            importlib.reload(main)
            assert main.PIPEIMOB_HTTP_TIMEOUT_SECONDS == 12
    finally:
        loop.close()
        asyncio.set_event_loop(None)

def test_get_db_session_exception_rollback():
    from main import get_db_session
    db_gen = get_db_session()
    mock_db = MagicMock()
    with patch("database.SessionLocal", return_value=mock_db):
        next(db_gen)
        try:
            db_gen.throw(ValueError("Simulated route exception"))
        except ValueError:
            pass
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

def test_contracts_control_sprint2_auth_and_filling(client_with_db):
    from unittest.mock import patch, MagicMock
    import uuid
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key
    from models.contracts_control import ContractsControlResponsible
    from repositories.contracts_control_repository import ContractsControlRepository
    from services.contracts_control_manual_service import ContractsControlManualService
    import main

    # Enable writes and allowlist
    import os
    os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
    os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"

    # Mock authenticated user
    main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
        "sub": "some_user", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
    }

    db = client_with_db
    client = TestClient(main.app)

    try:
        # Create some responsibles
        r1 = ContractsControlResponsible(id=uuid.uuid4(), name="Beta Responsible", normalized_name="beta responsible", active=True)
        r2 = ContractsControlResponsible(id=uuid.uuid4(), name="Alpha Responsible", normalized_name="alpha responsible", active=False)
        db.add_all([r1, r2])
        db.commit()

        # 1. GET active ones
        res = client.get("/api/contracts-control/responsibles")
        assert res.status_code == 200
        data = res.json()
        active_names = [r["name"] for r in data["responsibles"]]
        assert "Beta Responsible" in active_names
        assert "Alpha Responsible" not in active_names

        # GET with include_inactive=True when not admin (some_user is not in admin_subs)
        res = client.get("/api/contracts-control/responsibles?include_inactive=true")
        assert res.status_code == 403

        # Set to admin
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.get("/api/contracts-control/responsibles?include_inactive=true")
        assert res.status_code == 200
        data = res.json()
        assert len(data["responsibles"]) >= 2
        # Check order by normalized name (Alpha comes before Beta)
        idx_alpha = -1
        idx_beta = -1
        for idx, r in enumerate(data["responsibles"]):
            if r["name"] == "Alpha Responsible":
                idx_alpha = idx
            elif r["name"] == "Beta Responsible":
                idx_beta = idx
        assert idx_alpha != -1 and idx_beta != -1
        assert idx_alpha < idx_beta

        # 2. Test POST /api/contracts-control/responsibles
        # Non-admin user gets 403 Forbidden
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "regular_user", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Cristina"})
        assert res.status_code == 403

        # Admin user creates responsible successfully
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Cristina"})
        assert res.status_code == 200
        created = res.json()
        assert created["name"] == "Cristina"
        assert created["active"] is True
        cristina_id = created["id"]

        # Try duplicate name -> 409
        res = client.post("/api/contracts-control/responsibles", json={"name": "  cristina  "})
        assert res.status_code == 409

        # Try empty name -> 422
        res = client.post("/api/contracts-control/responsibles", json={"name": ""})
        assert res.status_code == 422

        # 3. Test PATCH /api/contracts-control/responsibles/{responsible_id}
        # Non-admin gets 403
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "regular_user", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.patch(f"/api/contracts-control/responsibles/{cristina_id}", json={"name": "Cristina M"})
        assert res.status_code == 403

        # Admin updates responsible name
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.patch(f"/api/contracts-control/responsibles/{cristina_id}", json={"name": "Cristina Maria"})
        assert res.status_code == 200
        updated = res.json()
        assert updated["name"] == "Cristina Maria"

        # Deactivate responsible
        res = client.patch(f"/api/contracts-control/responsibles/{cristina_id}", json={"active": False})
        assert res.status_code == 200
        assert res.json()["active"] is False

        # 4. Test PATCH /api/contracts-control/deals/{transaction_id}/manual-data
        mock_dataset = [
            {
                "tx": {
                    "transacao_unique_id_pipeimob": "tx_test_123",
                    "codigo_imovel": "IM100",
                    "agente_gestor": "Gestor A"
                },
                "status_at_period_end": "operations",
                "current_status": "operations",
                "modality": "financing",
                "modality_label": "Financiamento",
                "modality_source": "manual",
                "modality_confidence": "confirmed",
                "financing_bank": "Banco do Brasil",
                "financing_amount": 100000.0,
                "financing_ratio": 0.8,
                "modality_flags": [],
                "source_type": "standard",
                "source_type_label": "Padrão",
                "data_quality_flags": [],
                "duration_days": 10,
                "current_aging_days": 5,
                "aging_days_at_period_end": 5
            }
        ]

        async def mock_load_dataset(*args, **kwargs):
            return "demo", "mock", mock_dataset, 1, "stale"

        with patch("main.load_contracts_control_dataset", side_effect=mock_load_dataset):
            # Activate Cristina for test
            res = client.patch(f"/api/contracts-control/responsibles/{cristina_id}", json={"active": True})
            assert res.status_code == 200

            # Non-admin gets 403
            main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
                "sub": "regular_user", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
            }
            res = client.patch("/api/contracts-control/deals/tx_test_123/manual-data", json={"responsible_id": cristina_id, "version": 0})
            assert res.status_code == 403

            # Admin assigns responsible (version 0 -> 1)
            main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
                "sub": "admin_sub_1", "email": "admin@gralhaimoveis.com.br", "role": "authenticated"
            }
            res = client.patch("/api/contracts-control/deals/tx_test_123/manual-data", json={"responsible_id": cristina_id, "version": 0})
            assert res.status_code == 200
            assigned = res.json()
            assert assigned["version"] == 1
            assert assigned["responsible"]["id"] == cristina_id
            assert assigned["changed"] is True

            # Incompatible expected version -> 409
            res = client.patch("/api/contracts-control/deals/tx_test_123/manual-data", json={"responsible_id": cristina_id, "version": 0})
            assert res.status_code == 409

            # Admin updates again (version 1 -> 2, responsible_id=None)
            res = client.patch("/api/contracts-control/deals/tx_test_123/manual-data", json={"responsible_id": None, "version": 1})
            assert res.status_code == 200
            assert res.json()["version"] == 2
            assert res.json()["responsible"] is None

            # Re-assigning inactive responsible -> 422
            client.patch(f"/api/contracts-control/responsibles/{cristina_id}", json={"active": False})
            res = client.patch("/api/contracts-control/deals/tx_test_123/manual-data", json={"responsible_id": cristina_id, "version": 2})
            assert res.status_code == 422

            # Verify history entries preserve inactive responsible's name
            res = client.get("/api/contracts-control/deals/tx_test_123/manual-data/history")
            assert res.status_code == 200
            history = res.json()
            assert len(history) >= 2
            assert history[0]["new_responsible"]["current_name"] == "Cristina Maria"
            assert history[0]["new_responsible"]["active"] is False

        # 5. Test bulk attribution
        mock_dataset.append({
            "tx": {
                "transacao_unique_id_pipeimob": "tx_test_456",
                "codigo_imovel": "IM101",
                "agente_gestor": "Gestor B"
            },
            "status_at_period_end": "operations",
            "current_status": "operations",
            "modality": "financing",
            "modality_label": "Financiamento",
            "modality_source": "manual",
            "modality_confidence": "confirmed",
            "financing_bank": "Banco do Brasil",
            "financing_amount": 100000.0,
            "financing_ratio": 0.8,
            "modality_flags": [],
            "source_type": "standard",
            "source_type_label": "Padrão",
            "data_quality_flags": [],
            "duration_days": 10,
            "current_aging_days": 5,
            "aging_days_at_period_end": 5
        })

        with patch("main.load_contracts_control_dataset", side_effect=mock_load_dataset):
            # Create an active responsible
            res = client.post("/api/contracts-control/responsibles", json={"name": "Mariana"})
            assert res.status_code == 200
            mariana_id = res.json()["id"]

            # Test limit > 100 items
            large_items = [{"transaction_id": f"tx_{i}", "version": 0} for i in range(101)]
            res = client.post("/api/contracts-control/manual-data/bulk", json={"items": large_items, "responsible_id": mariana_id})
            assert res.status_code == 422

            # Test duplicate IDs
            dup_items = [
                {"transaction_id": "tx_test_123", "version": 2},
                {"transaction_id": "tx_test_123", "version": 2}
            ]
            res = client.post("/api/contracts-control/manual-data/bulk", json={"items": dup_items, "responsible_id": mariana_id})
            assert res.status_code == 422

            # Test valid bulk update: tx_test_123 (expected version 2) and tx_test_456 (expected version 0)
            valid_items = [
                {"transaction_id": "tx_test_123", "version": 2},
                {"transaction_id": "tx_test_456", "version": 0}
            ]
            res = client.post("/api/contracts-control/manual-data/bulk", json={"items": valid_items, "responsible_id": mariana_id})
            assert res.status_code == 200
            bulk_res = res.json()
            assert bulk_res["requested_count"] == 2
            assert bulk_res["updated_count"] == 2
            assert bulk_res["unchanged_count"] == 0
            assert any(item["transaction_id"] == "tx_test_123" and item["version"] == 3 and item["changed"] is True for item in bulk_res["items"])
            assert any(item["transaction_id"] == "tx_test_456" and item["version"] == 1 and item["changed"] is True for item in bulk_res["items"])

            # Test atomic rollback: one item fails, entire batch rolls back
            failed_items = [
                {"transaction_id": "tx_test_123", "version": 2}, # incorrect expected version (is 3)
                {"transaction_id": "tx_test_456", "version": 1}  # correct version (is 1)
            ]
            res = client.post("/api/contracts-control/manual-data/bulk", json={"items": failed_items, "responsible_id": mariana_id})
            assert res.status_code == 409

            # Confirm rollback: tx_test_456 is still version 1 (not updated to 2)
            from models.contracts_control import ContractsControlManualData
            md = db.get(ContractsControlManualData, "tx_test_456")
            assert md.version == 1

    finally:
        import os
        os.environ.pop("CONTRACTS_CONTROL_WRITES_ENABLED", None)
        os.environ.pop("CONTRACTS_CONTROL_ADMIN_SUBS", None)
        main.app.dependency_overrides.clear()

def test_contracts_control_manual_service_edge_cases(client_with_db):
    import pytest
    import uuid
    from services.contracts_control_manual_service import ContractsControlManualService
    db = client_with_db

    # 1. update_responsible responsible_not_found
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_responsible(db, uuid.uuid4(), name="Non Existent")
    assert str(exc.value) == "responsible_not_found"

    # Create one active responsible
    r1 = ContractsControlManualService.create_responsible(db, "First Resp")

    # 2. update_responsible empty_name
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_responsible(db, r1.id, name="")
    assert str(exc.value) == "empty_name"

    # Create a second responsible
    r2 = ContractsControlManualService.create_responsible(db, "Second Resp")

    # 3. update_responsible duplicate_name
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_responsible(db, r1.id, name="Second Resp")
    assert str(exc.value) == "duplicate_name"

    # 4. update_individual_attribution responsible_not_found
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_individual_attribution(db, "tx_1", uuid.uuid4(), 0, "actor")
    assert str(exc.value) == "responsible_not_found"

    # Deactivate r2
    ContractsControlManualService.update_responsible(db, r2.id, active=False)

    # 5. update_individual_attribution responsible_inactive
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_individual_attribution(db, "tx_1", r2.id, 0, "actor")
    assert str(exc.value) == "responsible_inactive"

    # 6. update_bulk_attribution items_empty
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_bulk_attribution(db, [], None, "actor", set())
    assert str(exc.value) == "items_empty"

    # 7. update_bulk_attribution items_limit_exceeded
    large_items = [{"transaction_id": f"tx_{i}", "version": 0} for i in range(101)]
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_bulk_attribution(db, large_items, None, "actor", set())
    assert str(exc.value) == "items_limit_exceeded"

    # 8. update_bulk_attribution empty_transaction_id
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_bulk_attribution(db, [{"transaction_id": "", "version": 0}], None, "actor", set())
    assert str(exc.value) == "empty_transaction_id"

    # 9. update_bulk_attribution duplicate_transaction_ids
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_bulk_attribution(db, [{"transaction_id": "tx_1", "version": 0}, {"transaction_id": "tx_1", "version": 0}], None, "actor", set())
    assert str(exc.value) == "duplicate_transaction_ids"

    # 10. update_bulk_attribution responsible_not_found
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_bulk_attribution(db, [{"transaction_id": "tx_1", "version": 0}], uuid.uuid4(), "actor", {"tx_1"})
    assert str(exc.value) == "responsible_not_found"

    # 11. update_bulk_attribution responsible_inactive
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_bulk_attribution(db, [{"transaction_id": "tx_1", "version": 0}], r2.id, "actor", {"tx_1"})
    assert str(exc.value) == "responsible_inactive"

    # 12. update_bulk_attribution transaction_not_found
    with pytest.raises(ValueError) as exc:
        ContractsControlManualService.update_bulk_attribution(db, [{"transaction_id": "tx_1", "version": 0}], None, "actor", set())
    assert str(exc.value) == "transaction_not_found:tx_1"

    # 13. Case A Idempotency: no record, responsible_id is None, version is 0
    md_case_a, changed_case_a = ContractsControlManualService.update_individual_attribution(
        db, "tx_not_exists_case_a", None, 0, "actor"
    )
    assert changed_case_a is False
    assert md_case_a.version == 0
    assert md_case_a.responsible_id is None

    # Create a record for case B
    r3 = ContractsControlManualService.create_responsible(db, "Third Resp")
    db.commit()
    md_initial, changed_initial = ContractsControlManualService.update_individual_attribution(
        db, "tx_case_b", r3.id, 0, "actor"
    )
    assert changed_initial is True
    assert md_initial.version == 1
    db.commit()

    # 14. Case B Idempotency: same responsible, expected version matches current version
    md_case_b, changed_case_b = ContractsControlManualService.update_individual_attribution(
        db, "tx_case_b", r3.id, 1, "actor"
    )
    assert changed_case_b is False
    assert md_case_b.version == 1

def test_contracts_control_temporary_config_validation(client_with_db):
    import os
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key
    import main

    db = client_with_db
    client = TestClient(main.app)

    def reset_env():
        to_remove = [k for k in main.app.dependency_overrides if k != main.get_db_session]
        for k in to_remove:
            main.app.dependency_overrides.pop(k, None)
        os.environ.pop("CONTRACTS_CONTROL_WRITES_ENABLED", None)
        os.environ.pop("CONTRACTS_CONTROL_ADMIN_SUBS", None)

    try:
        # A. Claim sub missing from token -> 401
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Test"})
        assert res.status_code == 401

        # B. Writes disabled (WRITES_ENABLED = false) -> 403
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "false"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Test"})
        assert res.status_code == 403

        # C. Invalid WRITES_ENABLED boolean format -> 503
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "invalid"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Test"})
        assert res.status_code == 503

        # D. Empty allowlist -> 503
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = ""
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Test"})
        assert res.status_code == 503

        # E. Allowlist with spaces -> 503
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1, admin_sub_2"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Test"})
        assert res.status_code == 503

        # F. Allowlist with duplicates -> 503
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1,admin_sub_1"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Test"})
        assert res.status_code == 503

        # G. Empty entries in allowlist -> 503
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1,,admin_sub_2"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Test"})
        assert res.status_code == 503

        # H. Authorized sub -> 200
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "admin_sub_1", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Unique Name Config"})
        assert res.status_code == 200

        # I. Unauthorized sub -> 403
        reset_env()
        os.environ["CONTRACTS_CONTROL_WRITES_ENABLED"] = "true"
        os.environ["CONTRACTS_CONTROL_ADMIN_SUBS"] = "admin_sub_1"
        main.app.dependency_overrides[main.verify_backend_api_key] = lambda: {
            "sub": "unauthorized_sub", "email": "test@gralhaimoveis.com.br", "role": "authenticated"
        }
        res = client.post("/api/contracts-control/responsibles", json={"name": "Unique Name Config 2"})
        assert res.status_code == 403

    finally:
        reset_env()

def test_contracts_control_creation_concurrency(client_with_db):
    import os
    import pytest
    from fastapi.testclient import TestClient
    from main import app, verify_backend_api_key
    from models.contracts_control import ContractsControlResponsible
    from services.contracts_control_manual_service import ContractsControlManualService
    import main

    db = client_with_db

    try:
        # Create an active responsible
        r1 = ContractsControlManualService.create_responsible(db, "Concurrency Resp")
        db.commit()

        # Simulate concurrent creation at version 0 for the same transaction ID "tx_concurrent_123"
        # First attempt: creates the record
        md1, changed1 = ContractsControlManualService.update_individual_attribution(
            db, "tx_concurrent_123", r1.id, 0, "actor_1"
        )
        assert changed1 is True
        assert md1.version == 1
        db.commit()

        # Second attempt concurrent execution:
        from repositories.contracts_control_repository import ContractsControlRepository
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(ValueError) as exc:
            from unittest.mock import patch
            with patch("repositories.contracts_control_repository.ContractsControlRepository.get_manual_data_by_transaction_id", return_value=None):
                ContractsControlManualService.update_individual_attribution(
                    db, "tx_concurrent_123", r1.id, 0, "actor_2"
                )
        assert str(exc.value) == "version_conflict"

    finally:
        main.app.dependency_overrides.clear()


def test_single_flight_diagnostics_scenarios():
    import asyncio
    import pytest
    from main import single_flight_registry

    # 1. Three parallel requests create exactly one task
    calls = 0
    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return "result"

    async def run_parallel():
        tasks = [
            single_flight_registry.execute("test_key", fetch),
            single_flight_registry.execute("test_key", fetch),
            single_flight_registry.execute("test_key", fetch),
        ]
        results = await asyncio.gather(*tasks)
        return results

    results = asyncio.run(run_parallel())
    assert results == ["result", "result", "result"]
    assert calls == 1

    # 2. Owner cancelled does not cancel task
    calls_cancelled = 0
    task_started = None
    async def fetch_cancelled():
        nonlocal calls_cancelled
        calls_cancelled += 1
        task_started.set()
        await asyncio.sleep(0.2)
        return "finished"

    async def run_cancelled_owner():
        nonlocal task_started
        task_started = asyncio.Event()
        owner_coro = single_flight_registry.execute("cancel_key", fetch_cancelled)
        owner_task = asyncio.create_task(owner_coro)
        await task_started.wait()

        owner_task.cancel()
        try:
            await owner_task
        except asyncio.CancelledError:
            pass

        shared_task = single_flight_registry.in_flight.get("cancel_key")
        assert shared_task is not None
        res = await shared_task
        assert res == "finished"
        assert calls_cancelled == 1

    asyncio.run(run_cancelled_owner())

    # 3. Waiter receives same exception
    async def fetch_fail():
        await asyncio.sleep(0.05)
        raise ValueError("fetch_error")

    async def run_waiter_fail():
        t1 = asyncio.create_task(single_flight_registry.execute("fail_key", fetch_fail))
        t2 = asyncio.create_task(single_flight_registry.execute("fail_key", fetch_fail))
        res1, res2 = None, None
        try:
            await t1
        except ValueError as e:
            res1 = str(e)
        try:
            await t2
        except ValueError as e:
            res2 = str(e)
        assert res1 == "fetch_error"
        assert res2 == "fetch_error"

    asyncio.run(run_waiter_fail())

    # 4. Waiter timeout does not cancel task
    waiter_timeout_done = None
    async def fetch_timeout():
        await asyncio.sleep(0.2)
        waiter_timeout_done.set()
        return "timeout_success"

    async def run_waiter_timeout():
        nonlocal waiter_timeout_done
        waiter_timeout_done = asyncio.Event()
        t1 = asyncio.create_task(single_flight_registry.execute("timeout_key", fetch_timeout, timeout=0.05))
        try:
            await t1
            assert False, "Should have timed out"
        except asyncio.TimeoutError:
            pass

        assert "timeout_key" in single_flight_registry.in_flight
        await waiter_timeout_done.wait()
        assert "timeout_key" not in single_flight_registry.in_flight

    asyncio.run(run_waiter_timeout())

    assert "test_key" not in single_flight_registry.in_flight
    assert "cancel_key" not in single_flight_registry.in_flight
    assert "fail_key" not in single_flight_registry.in_flight
    assert "timeout_key" not in single_flight_registry.in_flight


def test_contracts_control_dataset_warming_response(client_with_db):
    import os
    from unittest.mock import patch
    import main
    from main import contracts_control_cache, generate_contracts_control_cache_key
    from tests.test_main import client

    old_env = dict(os.environ)
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "mock_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "mock_secret"
    os.environ["CONTRACTS_CONTROL_WARMUP_WAIT_SECONDS"] = "0.1"
    os.environ["CONTRACTS_CONTROL_RETRY_AFTER_SECONDS"] = "20"
    os.environ["CONTRACTS_CONTROL_MAX_STALE_SECONDS"] = "1"

    cache_key = generate_contracts_control_cache_key("2020-01-01")
    contracts_control_cache.clear()

    import time
    def slow_fetch(*args, **kwargs):
        time.sleep(0.5)
        return [{"transacao_unique_id_pipeimob": "tx_1"}], 1

    try:
        with patch("main.fetch_all_pipeimob_transactions", side_effect=slow_fetch):
            res = client.get("/api/contracts-control/summary?data_inicio=2026-01-01&data_fim=2026-07-30&scope=operations")
            assert res.status_code == 503
            assert res.headers.get("Retry-After") == "20"
            body = res.json()
            assert body["code"] == "dataset_warming"
            assert body["error_code"] == "dataset_warming"
            assert body["retry_after_seconds"] == 20

            import asyncio
            async def wait_bg():
                while "pipeimob:raw" in main.single_flight_registry.in_flight:
                    await asyncio.sleep(0.05)
            asyncio.run(wait_bg())

            res2 = client.get("/api/contracts-control/summary?data_inicio=2026-01-01&data_fim=2026-07-30&scope=operations")
            assert res2.status_code == 200

    finally:
        os.environ.clear()
        os.environ.update(old_env)
        contracts_control_cache.clear()


def test_verify_backend_api_key_hs256_and_jwks():
    import os
    import jwt
    import time
    import pytest
    import main
    from main import verify_backend_api_key, AuthException, app

    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]

    secret = "super_secret_jwt_key_for_testing_32_bytes_long"
    payload = {
        "sub": "user_123",
        "email": "test@gralhaimoveis.com.br",
        "role": "authenticated",
        "iss": os.getenv("SUPABASE_ISSUER", "https://mock.supabase.co/auth/v1"),
        "aud": "authenticated",
        "exp": int(time.time()) + 3600
    }
    valid_token = jwt.encode(payload, secret, algorithm="HS256")

    old_env = dict(os.environ)
    os.environ["SUPABASE_JWT_SECRET"] = secret
    os.environ["APP_ENV"] = "production"

    try:
        import asyncio
        decoded = asyncio.run(verify_backend_api_key(f"Bearer {valid_token}"))
        assert decoded["sub"] == "user_123"
        assert decoded["email"] == "test@gralhaimoveis.com.br"

        # Invalid token signature
        bad_token = jwt.encode(payload, "wrong_secret_key_32_bytes_long_12345", algorithm="HS256")
        with pytest.raises(AuthException) as exc_info:
            asyncio.run(verify_backend_api_key(f"Bearer {bad_token}"))
        assert exc_info.value.status_code == 401
        assert exc_info.value.error_code == "invalid_access_token"

        # Missing Auth Header
        with pytest.raises(AuthException) as exc_info_no_auth:
            asyncio.run(verify_backend_api_key(None))
        assert exc_info_no_auth.value.status_code == 401
        assert exc_info_no_auth.value.error_code == "authentication_required"

    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_verify_backend_api_key_claims_and_algorithms():
    import os
    import jwt
    import time
    import pytest
    import main
    from main import verify_backend_api_key, AuthException, app

    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]

    secret = "super_secret_jwt_key_for_testing_32_bytes_long"
    now = int(time.time())
    base_payload = {
        "sub": "user_123",
        "email": "test@gralhaimoveis.com.br",
        "role": "authenticated",
        "iss": "https://expected.supabase.co/auth/v1",
        "aud": "authenticated",
        "exp": now + 3600
    }

    old_env = dict(os.environ)
    os.environ["SUPABASE_JWT_SECRET"] = secret
    os.environ["SUPABASE_ISSUER"] = "https://expected.supabase.co/auth/v1"
    os.environ["SUPABASE_JWT_AUDIENCE"] = "authenticated"
    os.environ["APP_ENV"] = "production"

    import asyncio

    try:
        # 1. Invalid issuer
        payload_bad_iss = dict(base_payload, iss="https://wrong.supabase.co/auth/v1")
        token_bad_iss = jwt.encode(payload_bad_iss, secret, algorithm="HS256")
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {token_bad_iss}"))
        assert exc.value.status_code == 401

        # 2. Invalid audience
        payload_bad_aud = dict(base_payload, aud="wrong_audience")
        token_bad_aud = jwt.encode(payload_bad_aud, secret, algorithm="HS256")
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {token_bad_aud}"))
        assert exc.value.status_code == 401

        # 3. Expired token
        payload_expired = dict(base_payload, exp=now - 3600)
        token_expired = jwt.encode(payload_expired, secret, algorithm="HS256")
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {token_expired}"))
        assert exc.value.status_code == 401

        # 4. Incorrect role
        payload_bad_role = dict(base_payload, role="anon")
        token_bad_role = jwt.encode(payload_bad_role, secret, algorithm="HS256")
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {token_bad_role}"))
        assert exc.value.status_code == 401

        # 5. Missing email
        payload_no_email = dict(base_payload)
        del payload_no_email["email"]
        token_no_email = jwt.encode(payload_no_email, secret, algorithm="HS256")
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {token_no_email}"))
        assert exc.value.status_code == 401

        # 6. Missing sub
        payload_no_sub = dict(base_payload)
        del payload_no_sub["sub"]
        token_no_sub = jwt.encode(payload_no_sub, secret, algorithm="HS256")
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {token_no_sub}"))
        assert exc.value.status_code == 401

        # 7. Disallowed algorithm ("none")
        token_none = jwt.encode(base_payload, "", algorithm="none")
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {token_none}"))
        assert exc.value.status_code == 401

        # 8. RS256 token without kid header
        token_rs256_nokid = jwt.encode(base_payload, "fake_rsa_key", algorithm="HS256")
        parts = token_rs256_nokid.split(".")
        fake_rs_token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9." + parts[1] + "." + parts[2]
        with pytest.raises(AuthException) as exc:
            asyncio.run(verify_backend_api_key(f"Bearer {fake_rs_token}"))
        assert exc.value.status_code == 401

    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_strategy_isolation_suite():
    import os
    import jwt
    import time
    import pytest
    from unittest.mock import patch, MagicMock
    import main
    from main import verify_backend_api_key, AuthException, app
    from fastapi import HTTPException

    if verify_backend_api_key in app.dependency_overrides:
        del app.dependency_overrides[verify_backend_api_key]

    old_env = dict(os.environ)
    secret = "super_secret_jwt_key_for_testing_32_bytes_long"
    now = int(time.time())
    base_payload = {
        "sub": "user_123",
        "email": "test@gralhaimoveis.com.br",
        "role": "authenticated",
        "iss": "https://mock.supabase.co/auth/v1",
        "aud": "authenticated",
        "exp": now + 3600
    }

    try:
        os.environ["SUPABASE_JWT_SECRET"] = secret
        os.environ["SUPABASE_ISSUER"] = "https://mock.supabase.co/auth/v1"
        os.environ["SUPABASE_JWT_AUDIENCE"] = "authenticated"
        os.environ["APP_ENV"] = "production"

        import asyncio

        # 1. HS256 valid token does NOT call get_jwk_client
        hs256_valid = jwt.encode(base_payload, secret, algorithm="HS256")
        with patch("main.get_jwk_client") as mock_jwk:
            res = asyncio.run(verify_backend_api_key(f"Bearer {hs256_valid}"))
            assert res["sub"] == "user_123"
            mock_jwk.assert_not_called()

        # 2. HS256 invalid signature returns 401 and does NOT call get_jwk_client
        hs256_invalid = jwt.encode(base_payload, "wrong_secret_32_bytes_long_12345", algorithm="HS256")
        with patch("main.get_jwk_client") as mock_jwk:
            with pytest.raises(AuthException) as exc:
                asyncio.run(verify_backend_api_key(f"Bearer {hs256_invalid}"))
            assert exc.value.status_code == 401
            mock_jwk.assert_not_called()

        # 3. RS256 token requires JWKS and does NOT use SUPABASE_JWT_SECRET
        os.environ["SUPABASE_JWKS_URL"] = "https://mock.supabase.co/auth/v1/.well-known/jwks.json"
        rs256_token = "eyJhbGciOiJSUzI1NiIsImtpZCI6Im1vY2tfa2lkIn0.eyJzdWIiOiJ1c2VyLTEyMyIsInJvbGUiOiJhdXRoZW50aWNhdGVkIiwiZW1haWwiOiJ0ZXN0QHRlc3QuY29tIiwiaXNzIjoiaHR0cHM6Ly9tb2NrLnN1cGFiYXNlLmNvL2F1dGgvdjEiLCJhdWQiOiJhdXRoZW50aWNhdGVkIiwiZXhwIjo5OTk5OTk5OTk5fQ.c2lnbmF0dXJl"
        mock_client = MagicMock()
        mock_key = MagicMock()
        mock_key.kid = "mock_kid"
        mock_key.key = "public_rsa_key"
        mock_jwk_set = MagicMock()
        mock_jwk_set.keys = [mock_key]
        mock_client.get_jwk_set.return_value = mock_jwk_set
        mock_client.get_signing_key_from_jwt.return_value = mock_key

        with patch("main.get_jwk_client", return_value=mock_client):
            with patch("jwt.decode", return_value=base_payload):
                res = asyncio.run(verify_backend_api_key(f"Bearer {rs256_token}"))
                assert res["sub"] == "user_123"


        # 4. Missing SUPABASE_ISSUER in production raises 500
        os.environ.pop("SUPABASE_ISSUER", None)
        with pytest.raises(HTTPException) as exc_500:
            asyncio.run(verify_backend_api_key(f"Bearer {hs256_valid}"))
        assert exc_500.value.status_code == 500
        assert "SUPABASE_ISSUER environment variable is required" in exc_500.value.detail

    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_version_endpoint_suite():
    import os
    from tests.test_main import client

    old_env = dict(os.environ)

    try:
        # 1. Production mode never returns branch
        os.environ["APP_ENV"] = "production"
        os.environ["RENDER_GIT_COMMIT"] = "commit123"
        os.environ["RENDER_GIT_BRANCH"] = "main"
        res = client.get("/api/version")
        assert res.status_code == 200
        body = res.json()
        assert body["commit_hash"] == "commit123"
        assert body["app_env"] == "production"
        assert "branch" not in body

        # 2. Staging mode returns branch when RENDER_GIT_BRANCH exists
        os.environ["APP_ENV"] = "staging"
        os.environ["RENDER_GIT_COMMIT"] = "commit456"
        os.environ["RENDER_GIT_BRANCH"] = "feature-branch"
        res = client.get("/api/version")
        assert res.status_code == 200
        body = res.json()
        assert body["commit_hash"] == "commit456"
        assert body["app_env"] == "staging"
        assert body["branch"] == "feature-branch"

        # 3. Staging mode omits branch when RENDER_GIT_BRANCH is missing
        os.environ.pop("RENDER_GIT_BRANCH", None)
        res = client.get("/api/version")
        assert res.status_code == 200
        body = res.json()
        assert body["commit_hash"] == "commit456"
        assert body["app_env"] == "staging"
        assert "branch" not in body

        # 4. Absence of RENDER_GIT_COMMIT returns "unknown"
        os.environ.pop("RENDER_GIT_COMMIT", None)
        res = client.get("/api/version")
        assert res.status_code == 200
        body = res.json()
        assert body["commit_hash"] == "unknown"

    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_database_hardening_suite():
    import os
    import pytest
    from fastapi import HTTPException
    import main
    from main import get_db_session

    old_env = dict(os.environ)

    try:
        # 1. Missing DATABASE_URL in production raises 500 in get_db_session
        os.environ["APP_ENV"] = "production"
        os.environ.pop("DATABASE_URL", None)
        gen = get_db_session()
        with pytest.raises(HTTPException) as exc_500:
            next(gen)
        assert exc_500.value.status_code == 500
        assert "DATABASE_URL environment variable is required" in exc_500.value.detail

        # 2. Local environment allows missing DATABASE_URL and yields None when SessionLocal is None
        os.environ["APP_ENV"] = "development"
        with patch("database.SessionLocal", None):
            gen_local = get_db_session()
            res = next(gen_local)
            assert res is None


    finally:
        os.environ.clear()
        os.environ.update(old_env)








def test_contracts_control_max_stale_limit_and_fallback(client_with_db):
    import os
    import time
    from unittest.mock import patch
    import main
    from main import contracts_control_cache, generate_contracts_control_cache_key
    from tests.test_main import client

    old_env = dict(os.environ)
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "mock_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "mock_secret"
    os.environ["CONTRACTS_CONTROL_WARMUP_WAIT_SECONDS"] = "0.1"
    os.environ["CONTRACTS_CONTROL_MAX_STALE_SECONDS"] = "2"

    cache_key = generate_contracts_control_cache_key("2020-01-01")
    contracts_control_cache.clear()

    calls = 0
    def mock_fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            time.sleep(0.5)
        return [{"transacao_unique_id_pipeimob": "tx_stale"}], 1

    try:
        with patch("main.fetch_all_pipeimob_transactions", side_effect=mock_fetch):
            # First request: force refresh to execute fetch synchronously and warm the cache
            res = client.get("/api/contracts-control/summary?data_inicio=2026-01-01&data_fim=2026-07-30&scope=operations&refresh=true")
            assert res.status_code == 200

            now = time.time()
            contracts_control_cache.cache[cache_key] = (
                ([{"transacao_unique_id_pipeimob": "tx_expired"}], 1),
                now - 100,
                now - 50,
                now - 10
            )
            contracts_control_cache.clear_endpoint_caches()

            res_expired = client.get("/api/contracts-control/summary?data_inicio=2026-01-01&data_fim=2026-07-30&scope=operations")
            assert res_expired.status_code == 503
            assert res_expired.json()["code"] == "dataset_warming"

    finally:
        os.environ.clear()
        os.environ.update(old_env)
        contracts_control_cache.clear()


def test_responsibles_decoupled_from_pipeimob_cache_and_warming():
    from fastapi import HTTPException
    from fastapi.testclient import TestClient
    from main import app, contracts_control_cache
    from models.contracts_control import Base, ContractsControlResponsible
    import database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sqlalchemy.pool import StaticPool

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    from sqlalchemy import event
    @event.listens_for(test_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s is not None else None)

    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(bind=test_engine)

    # Seed active responsibles in test DB
    db = TestingSessionLocal()
    resps = ["Guilherme", "Cristina", "Carol", "Laise"]
    for r in resps:
        db.add(ContractsControlResponsible(name=r, normalized_name=r.lower(), active=True))
    db.commit()
    db.close()

    def get_test_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    import main as main_module

    app.dependency_overrides[main_module.get_db_session] = get_test_db
    contracts_control_cache.clear()
    client = TestClient(app)

    auth_headers = {"Authorization": "Bearer mock_jwt_token"}

    try:
        # 1. Unauthenticated request to /responsibles should return 401
        res_unauth = client.get("/api/contracts-control/responsibles")
        assert res_unauth.status_code == 401

        # 2. When Pipeimob cache is cold, /responsibles returns 200 with 4 active responsibles
        main_module.app.dependency_overrides[main_module.verify_backend_api_key] = lambda: {
            "sub": "test-user-id",
            "role": "authenticated",
            "email": "test@gralhaimoveis.com.br"
        }
        with patch("main.load_contracts_control_dataset", side_effect=RuntimeError("load_contracts_control_dataset should NOT be called by /responsibles")):
            res_resp = client.get("/api/contracts-control/responsibles", headers=auth_headers)
            assert res_resp.status_code == 200
            data = res_resp.json()
            names = [r["name"] for r in data["responsibles"]]
            assert sorted(names) == ["Carol", "Cristina", "Guilherme", "Laise"]

        # 3. include_inactive=true requires admin credentials
        with patch("main.require_contracts_control_temporary_admin", side_effect=HTTPException(status_code=403, detail="Forbidden")):
            res_admin = client.get("/api/contracts-control/responsibles?include_inactive=true", headers=auth_headers)
            assert res_admin.status_code == 403

    finally:
        app.dependency_overrides.clear()
        contracts_control_cache.clear()
