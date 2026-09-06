"""Automated endpoint tests for Vista organizational coverage diagnostic (v3)."""

from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

import main
from main import app, verify_backend_api_key
from services.vista_organization_client import VistaOrganizationAPIError
from services.vista_sales_client import VistaSalesConfigurationError


class FakeVistaOrganizationClient:
    def __init__(self, users=None, deals=None, circuit_broken=False, probed=None, user_comp=None, deal_comp=None):
        self.created_field = "DataInicial"
        self._users = users or [
            {"user_id": "101", "role_type": "broker", "team_id": "24", "manager_id": "5", "agency_id": "1", "is_active": True},
            {"user_id": "102", "role_type": "broker", "team_id": "24", "manager_id": "5", "agency_id": "1", "is_active": True},
            {"user_id": "5", "role_type": "manager", "team_id": "24", "agency_id": "1", "is_active": True},
        ]
        self._deals = deals or [
            {"deal_id": "d1", "broker_id": "101", "team_id": "24", "manager_id": "5", "agency_id": "1", "deal_date": "2026-08-01"}
        ]
        self._circuit_broken = circuit_broken
        self._probed = probed or {
            "accepted": ["Codigo", "CodigoEquipe", "CodigoGerente", "CodigoAgencia", "Status", "Ativo"],
            "rejected": [],
        }
        self._user_comp = user_comp or {"pages_reported": 1, "pages_fetched": 1, "records_fetched": 3, "truncated": False, "complete": True}
        self._deal_comp = deal_comp or {"pages_reported": 1, "pages_fetched": 1, "records_fetched": 1, "truncated": False, "complete": True}

    def fetch_anonymized_users(self, max_pages=5):
        return self._users, self._user_comp

    def fetch_bounded_deal_associations(self, start_date, end_date, pipe_id=None, max_pages=5):
        return self._deals, self._deal_comp

    def is_circuit_broken(self):
        return self._circuit_broken

    def get_probed_fields(self):
        return self._probed


@pytest.fixture(autouse=True)
def clear_vista_org_coverage_cache():
    main.vista_org_coverage_cache.clear()
    yield
    main.vista_org_coverage_cache.clear()


def test_org_coverage_endpoint_requires_auth():
    client = TestClient(app)
    response = client.get("/api/vista/diagnostics/organizational-coverage")
    assert response.status_code in (401, 403)


def test_org_coverage_endpoint_success_with_bounded_cohort_and_completeness():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake = FakeVistaOrganizationClient()
    try:
        with patch("main.VistaOrganizationClient.from_env", return_value=fake):
            response = TestClient(app).get(
                "/api/vista/diagnostics/organizational-coverage"
                "?data_inicio=2026-08-01&data_fim=2026-08-31&max_pages=3"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["contract_version"] == "1.0"
    assert payload["diagnostic_target"] == "vista_organizational_stable_id_coverage"
    assert payload["overall_status"] == "ready"
    assert payload["completeness"]["complete"] is True
    assert payload["completeness"]["truncated"] is False
    assert payload["period"] == {"start": "2026-08-01", "end": "2026-08-31", "basis": "DataInicial"}
    assert payload["semantics"]["snapshot_type"] == "point_in_time_cohort"
    assert payload["semantics"]["event_history_preserved"] is False
    assert payload["privacy_guarantee"]["only_aggregate_counts"] is True
    assert response.headers["X-Diagnostic-Contract"] == "1.0"
    assert response.headers["X-Diagnostic-Status"] == "ready"
    assert response.headers["X-Data-Mode"] == "live"


def test_org_coverage_endpoint_truncated_snapshot_caps_to_partial():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake = FakeVistaOrganizationClient(
        user_comp={"pages_reported": 10, "pages_fetched": 3, "records_fetched": 150, "truncated": True, "complete": False}
    )
    try:
        with patch("main.VistaOrganizationClient.from_env", return_value=fake):
            response = TestClient(app).get("/api/vista/diagnostics/organizational-coverage?max_pages=3")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["completeness"]["truncated"] is True
    assert payload["overall_status"] == "partial"
    assert response.headers["X-Diagnostic-Status"] == "partial"


def test_org_coverage_endpoint_rejects_single_date():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    try:
        response = TestClient(app).get(
            "/api/vista/diagnostics/organizational-coverage?data_inicio=2026-08-01"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert "Both data_inicio and data_fim must be provided together" in response.json()["detail"]


def test_org_coverage_endpoint_caching_and_refresh():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake = FakeVistaOrganizationClient()
    try:
        with patch("main.VistaOrganizationClient.from_env", return_value=fake):
            client = TestClient(app)
            r1 = client.get("/api/vista/diagnostics/organizational-coverage")
            r2 = client.get("/api/vista/diagnostics/organizational-coverage")
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.headers["X-Diagnostic-Cache"] == "miss"
            assert r2.headers["X-Diagnostic-Cache"] == "fresh"

            r3 = client.get("/api/vista/diagnostics/organizational-coverage?refresh=true")
            assert r3.status_code == 200
            assert r3.headers["X-Diagnostic-Cache"] == "miss"
    finally:
        app.dependency_overrides.clear()


def test_org_coverage_endpoint_circuit_broken_sanitized():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake = FakeVistaOrganizationClient(circuit_broken=True)
    fake.fetch_anonymized_users = lambda **kw: (_ for _ in ()).throw(
        VistaOrganizationAPIError("Circuit open", "vista_circuit_broken")
    )
    try:
        with patch("main.VistaOrganizationClient.from_env", return_value=fake):
            response = TestClient(app).get("/api/vista/diagnostics/organizational-coverage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_status"] == "blocked"
    assert payload["circuit_breaker"]["triggered"] is True
    assert "circuit_breaker_triggered_after_two_failures" in payload["blocks_found"]


def test_org_coverage_endpoint_deal_query_error_preserves_error_and_caps_to_partial():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake = FakeVistaOrganizationClient()
    fake.fetch_bounded_deal_associations = lambda **kw: (_ for _ in ()).throw(
        VistaOrganizationAPIError("Deal query error", "vista_deal_query_failed")
    )
    try:
        with patch("main.VistaOrganizationClient.from_env", return_value=fake):
            response = TestClient(app).get(
                "/api/vista/diagnostics/organizational-coverage?data_inicio=2026-08-01&data_fim=2026-08-31"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["sources"]["users"]["successful"] is True
    assert payload["sources"]["deals"]["requested"] is True
    assert payload["sources"]["deals"]["successful"] is False
    assert payload["sources"]["deals"]["error_code"] == "vista_deal_query_failed"
    assert payload["completeness"]["complete"] is False
    assert payload["overall_status"] == "partial"
    assert "one_or_more_requested_sources_failed" in payload["blocks_found"]


def test_org_coverage_endpoint_zero_fields_negotiated():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake = FakeVistaOrganizationClient(probed={"accepted": [], "rejected": ["BadField1"]})
    try:
        with patch("main.VistaOrganizationClient.from_env", return_value=fake):
            response = TestClient(app).get("/api/vista/diagnostics/organizational-coverage")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["field_negotiation"]["supported"] is False
    assert payload["completeness"]["supported"] is False
    assert payload["completeness"]["complete"] is False
    assert payload["overall_status"] == "blocked"
    assert "no_supported_fields_negotiated" in payload["blocks_found"]
