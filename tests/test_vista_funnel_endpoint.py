from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app, verify_backend_api_key


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
