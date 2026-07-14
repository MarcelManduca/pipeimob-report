import os
import sys
from datetime import datetime, timezone
from fastapi.testclient import TestClient

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Clean environment credentials to verify application runs without credentials configured
os.environ.pop("PIPEIMOB_API_KEY", None)
os.environ.pop("PIPEIMOB_SECRET_KEY", None)

# Force development environment for local localhost CORS tests
os.environ["APP_ENV"] = "development"
os.environ["ALLOWED_ORIGINS"] = "https://lovable-test-origin.app"

from main import app

client = TestClient(app)

def test_app_starts_without_credentials():
    # Verify app initialization succeeds without environment variables
    assert app is not None

def test_get_health_status_code_200():
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "pipeimob-report"
    assert data["version"] == "0.1.0"
    assert data["api_version"] == "v2"
    assert data["pipeimob_connection"] == "pending"

def test_get_health_no_secrets_exposed():
    response = client.get("/api/health")
    data_str = response.text
    # Ensure no common secrets or sensitive config words are output
    assert "key" not in data_str.lower()
    assert "secret" not in data_str.lower()
    assert "token" not in data_str.lower()
    assert "env" not in data_str.lower()

def test_get_health_timestamp_valid_utc():
    response = client.get("/api/health")
    data = response.json()
    timestamp_str = data["timestamp"]
    
    # Assert ISO-8601 ends with Z indicating UTC timezone
    assert timestamp_str.endswith("Z")
    
    # Verify ISO-8601 parsing succeeds
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
    assert resource["pipeimob_endpoint"] is None  # Must remain null due to divergence
    assert resource["status"] == "pending_auth_confirmation"
    assert resource["implemented"] is False
    assert resource["validated"] is False
    assert resource["primary_key"] == "transacao_unique_id_pipeimob"

def test_get_catalog_contains_expected_fields():
    response = client.get("/api/catalog")
    resource = response.json()["resources"][0]
    
    expected_fields = [
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
    ]
    for field in expected_fields:
        assert field in resource["available_fields"]

def test_get_catalog_contains_expected_filters():
    response = client.get("/api/catalog")
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

def test_cors_authorized_origin():
    # Test normal request from an authorized origin
    headers = {"Origin": "https://lovable-test-origin.app"}
    response = client.get("/api/health", headers=headers)
    assert response.headers.get("access-control-allow-origin") == "https://lovable-test-origin.app"

def test_cors_authorized_localhost_in_dev():
    # Verify localhost and 127.0.0.1 are explicitly allowed in development env
    headers = {"Origin": "http://localhost:5173"}
    response = client.get("/api/health", headers=headers)
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

def test_cors_unauthorized_origin():
    # Test request from an unauthorized origin (must not return Access-Control-Allow-Origin header)
    headers = {"Origin": "https://unauthorized-domain.com"}
    response = client.get("/api/health", headers=headers)
    assert "access-control-allow-origin" not in response.headers

def test_cors_preflight_options():
    # Test OPTIONS preflight request for authorized origin
    headers = {
        "Origin": "https://lovable-test-origin.app",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Content-Type",
    }
    response = client.options("/api/health", headers=headers)
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://lovable-test-origin.app"
    assert "GET" in response.headers.get("access-control-allow-methods", "")

def test_openapi_includes_endpoints_and_schemas():
    response = client.get("/openapi.json")
    assert response.status_code == 200
    openapi_data = response.json()
    
    # Check paths
    paths = openapi_data["paths"]
    assert "/api/health" in paths
    assert "/api/catalog" in paths
    
    # Check health schemas
    schemas = openapi_data["components"]["schemas"]
    assert "HealthResponse" in schemas
    assert "CatalogResponse" in schemas
    assert "ResourceCatalog" in schemas
