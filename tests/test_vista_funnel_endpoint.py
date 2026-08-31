from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main
from main import app, verify_backend_api_key
from services.vista_sales_client import VistaSalesAPIError


class FakeVistaFunnelClient:
    created_field = "DataInicial"

    def fetch_created_deals(self, start_date, end_date):
        return [
            {
                "deal_id": "deal-1",
                "created_at": "2026-08-01",
                "status": "Aberto",
                "stage_name": "Proposta",
                "responsible": "Gerente Um",
            },
            {
                "deal_id": "deal-2",
                "created_at": "2026-08-02",
                "status": "Perdido",
                "stage_name": "Proposta",
            },
        ]


@pytest.fixture(autouse=True)
def clear_vista_funnel_cache():
    main.vista_funnel_cache.clear()
    yield
    main.vista_funnel_cache.clear()


def test_vista_funnel_endpoint_returns_aggregate_semantics():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    try:
        with patch(
            "main.VistaFunnelClient.from_env",
            return_value=FakeVistaFunnelClient(),
        ):
            response = TestClient(app).get(
                "/api/vista/funnel/cohort"
                "?data_inicio=2026-08-01&data_fim=2026-08-30"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "vista_negocios_listar"
    assert payload["period"]["basis"] == "DataInicial"
    assert payload["summary"]["created_deals"] == 2
    assert (
        payload["summary"]["proposal"]["created_deals_currently_in_proposal"]
        == 2
    )
    assert payload["summary"]["proposal"]["proposals_generated_in_period"] is None
    assert payload["contract_version"] == "1.1"
    assert payload["summary"]["stage_status_breakdown"] == [
        {
            "stage": "Proposta",
            "deals_count": 2,
            "status_breakdown": [
                {"status": "Aberto", "deals_count": 1},
                {"status": "Perdido", "deals_count": 1},
            ],
        }
    ]
    assert (
        payload["summary"]["proposal"]
        ["created_deals_in_proposal_stage_with_open_status"]
        == 1
    )
    assert payload["semantics"]["stage_entry_events_available"] is False
    assert response.headers["X-Funnel-Semantics"] == "created_deals_current_stage"
    assert response.headers["X-Funnel-Contract"] == "1.1"
    assert response.headers["X-Funnel-Cache"] == "miss"


def test_vista_funnel_endpoint_reuses_recent_result():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake_client = FakeVistaFunnelClient()
    calls = 0
    original_fetch = fake_client.fetch_created_deals

    def counted_fetch(start_date, end_date):
        nonlocal calls
        calls += 1
        return original_fetch(start_date, end_date)

    fake_client.fetch_created_deals = counted_fetch
    try:
        with patch(
            "main.VistaFunnelClient.from_env",
            return_value=fake_client,
        ):
            client = TestClient(app)
            first = client.get(
                "/api/vista/funnel/cohort"
                "?data_inicio=2026-08-01&data_fim=2026-08-30"
            )
            second = client.get(
                "/api/vista/funnel/cohort"
                "?data_inicio=2026-08-01&data_fim=2026-08-30"
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["X-Funnel-Cache"] == "miss"
    assert second.headers["X-Funnel-Cache"] == "fresh"
    assert calls == 1


def test_vista_funnel_endpoint_serves_recent_stale_result_on_vista_failure():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake_client = FakeVistaFunnelClient()
    try:
        with patch("main.VistaFunnelClient.from_env", return_value=fake_client):
            client = TestClient(app)
            first = client.get(
                "/api/vista/funnel/cohort"
                "?data_inicio=2026-08-01&data_fim=2026-08-30"
            )
            cache_key = (
                "vista_funnel_cohort",
                "1.1",
                "2026-08-01",
                "2026-08-30",
            )
            with main.vista_funnel_cache.lock:
                payload, _, stale_until = main.vista_funnel_cache.cache[cache_key]
                main.vista_funnel_cache.cache[cache_key] = (
                    payload,
                    0,
                    stale_until,
                )

            fake_client.fetch_created_deals = lambda *_: (_ for _ in ()).throw(
                VistaSalesAPIError("temporary")
            )
            second = client.get(
                "/api/vista/funnel/cohort"
                "?data_inicio=2026-08-01&data_fim=2026-08-30"
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.headers["X-Funnel-Cache"] == "stale-if-error"
    assert second.headers["X-Data-Mode"] == "cached"
    assert second.headers["X-Funnel-Semantics"] == "created_deals_current_stage"


def test_vista_funnel_endpoint_propagates_sanitized_failure_code():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    fake_client = FakeVistaFunnelClient()
    failure = VistaSalesAPIError("safe failure")
    failure.error_code = "vista_http_429"
    fake_client.fetch_created_deals = lambda *_: (_ for _ in ()).throw(failure)
    try:
        with patch("main.VistaFunnelClient.from_env", return_value=fake_client):
            response = TestClient(app).get(
                "/api/vista/funnel/cohort"
                "?data_inicio=2026-08-01&data_fim=2026-08-30"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.headers["X-Funnel-Error"] == "vista_http_429"
    assert response.headers["Retry-After"] == "60"


def test_vista_funnel_endpoint_rejects_period_over_one_year():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    try:
        response = TestClient(app).get(
            "/api/vista/funnel/cohort"
            "?data_inicio=2025-01-01&data_fim=2026-08-30"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"] == "Funnel period cannot exceed 366 days"
