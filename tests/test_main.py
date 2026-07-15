import os
import sys
from datetime import datetime, timezone
from fastapi.testclient import TestClient

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Force development environment for local localhost CORS tests
os.environ["APP_ENV"] = "development"
os.environ["ALLOWED_ORIGINS"] = "https://lovable-test-origin.app"

# Import mock data to assert absence of real spreadsheet names in codebase mocks
from mock_data import MOCK_TRANSACTIONS
from main import app

client = TestClient(app)

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
    assert resource["pipeimob_endpoint"] is None
    assert resource["status"] == "implemented_demo_pending_live_validation"
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

def test_unconfigured_endpoints_return_503():
    os.environ["APP_ENV"] = "production"
    os.environ.pop("PIPEIMOB_DATA_MODE", None)
    
    # Endpoints must fail with 503 while unconfigured, never returning demo data silently
    response = client.get("/api/transactions")
    assert response.status_code == 503
    assert "Configuration pending" in response.json()["detail"]
    
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 503
    assert "Configuration pending" in response.json()["detail"]

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
        "data_arquivamento_fim"
    ]
    for filter_name in expected_filters:
        assert filter_name in resource["supported_filters"]
        
    assert "data_inicio_criacao" in resource["filters_api_direct"]
    assert "data_fim_criacao" in resource["filters_local_backend"]

def test_catalog_status_states():
    # 1. Demo Mode
    os.environ["PIPEIMOB_DATA_MODE"] = "demo"
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_demo_pending_live_validation"
    
    # 2. Live Mode (no credentials)
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ.pop("PIPEIMOB_API_KEY", None)
    os.environ.pop("PIPEIMOB_SECRET_KEY", None)
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_pending_live_configuration"
    
    # 3. Unconfigured Mode (production)
    os.environ["APP_ENV"] = "production"
    os.environ.pop("PIPEIMOB_DATA_MODE", None)
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_pending_live_configuration"
    
    # 4. Live Mode (with credentials configured but validation pending)
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    response = client.get("/api/catalog")
    assert response.json()["resources"][0]["status"] == "implemented_pending_live_validation"

def test_live_mode_without_credentials_returns_error():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ.pop("PIPEIMOB_API_KEY", None)
    os.environ.pop("PIPEIMOB_SECRET_KEY", None)
    
    response = client.get("/api/transactions")
    assert response.status_code == 400
    assert "Configuration pending" in response.json()["detail"]

def test_headers_credentials_are_ignored():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ.pop("PIPEIMOB_API_KEY", None)
    os.environ.pop("PIPEIMOB_SECRET_KEY", None)
    
    headers = {
        "X-API-Key": "client_supplied_key",
        "X-Secret-Key": "client_supplied_secret"
    }
    response = client.get("/api/transactions", headers=headers)
    assert response.status_code == 400
    assert "Configuration pending" in response.json()["detail"]

def test_live_mode_failure_does_not_return_mock():
    os.environ["PIPEIMOB_DATA_MODE"] = "live"
    os.environ["PIPEIMOB_API_KEY"] = "fake_key"
    os.environ["PIPEIMOB_SECRET_KEY"] = "fake_secret"
    
    response = client.get("/api/transactions")
    assert response.status_code == 503
    assert "Pipeimob connection failed" in response.json()["detail"]

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
    
    schemas = openapi_data["components"]["schemas"]
    assert "TransactionsListResponse" in schemas
    assert "DashboardSummaryResponse" in schemas
