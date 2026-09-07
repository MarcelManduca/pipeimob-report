"""Endpoint tests for the aggregate Pipeimob organizational diagnostic."""

from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from main import app, require_vista_diagnostic_admin


def _aggregates(status="attention"):
    return {
        "data_quality": {
            "transaction_count": 2,
            "summary": {
                "status": status,
                "distinct_agents_count": 2,
                "compliant_agents_count": 0,
                "affected_agents_count": 1,
                "review_only_agents_count": 1,
                "agent_compliance_ratio": 0,
                "transaction_compliance_ratio": 0,
                "unassigned_manager_transactions_count": 0,
            },
            "teams": {
                "source_fields": {
                    "primary": "agente_gestor_grupos_a_que_pertence",
                    "primary_type": "array_of_group_ids",
                },
                "configuration_status": "missing",
                "official_teams_configured": False,
                "issues": [
                    {
                        "id": "configuration_mapping_required",
                        "severity": "review",
                        "title": "must not leak",
                        "affected_agents_count": 2,
                        "affected_transactions_count": 2,
                    }
                ],
                "affected_agents": [{"agent_name": "must not leak"}],
            },
        }
    }


def test_pipeimob_org_diagnostic_is_aggregate_and_measures_observed_ids():
    dataset = [
        {
            "agente_gestor": "Pessoa Um",
            "agente_gestor_grupos_a_que_pertence": ["group-1", "group-2"],
            "agente_gestor_grupo_filial": "Filial A",
        },
        {
            "agente_gestor": "Pessoa Dois",
            "agente_gestor_grupos_a_que_pertence": [],
            "agente_gestor_grupo_filial": "",
        },
    ]

    async def fake_load(**_kwargs):
        return "live", "pipeimob_api_v2", dataset, 1, "miss"

    app.dependency_overrides[require_vista_diagnostic_admin] = lambda: "admin"
    try:
        with patch("main.load_transactions_dataset", side_effect=fake_load), patch(
            "main.compute_dashboard_aggregates", return_value=_aggregates()
        ):
            response = TestClient(app).get(
                "/api/pipeimob/diagnostics/organizational-coverage"
                "?data_inicio=2026-01-01&data_fim=2026-09-07"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["automation_status"] == "blocked"
    assert payload["group_contract"]["stable_group_ids_observed"] is True
    assert payload["group_contract"]["distinct_group_ids_observed_count"] == 2
    assert payload["group_contract"]["transactions_with_group_ids_count"] == 1
    assert payload["group_contract"]["directory_source_available"] is False
    assert payload["group_contract"]["manual_configuration_is_authoritative_source"] is False
    assert "group_type_directory_not_available" in payload["automation_blocks"]
    assert "Pessoa Um" not in response.text
    assert "must not leak" not in response.text
    assert response.headers["X-Diagnostic-Status"] == "blocked"


def test_pipeimob_org_diagnostic_does_not_claim_unobserved_ids():
    dataset = [{"agente_gestor_grupos_a_que_pertence": []}]

    async def fake_load(**_kwargs):
        return "live", "pipeimob_api_v2", dataset, 1, "miss"

    app.dependency_overrides[require_vista_diagnostic_admin] = lambda: "admin"
    try:
        with patch("main.load_transactions_dataset", side_effect=fake_load), patch(
            "main.compute_dashboard_aggregates", return_value=_aggregates("critical")
        ):
            response = TestClient(app).get(
                "/api/pipeimob/diagnostics/organizational-coverage"
                "?data_inicio=2026-08-01&data_fim=2026-08-31"
            )
    finally:
        app.dependency_overrides.clear()

    payload = response.json()
    assert payload["group_contract"]["stable_group_ids_observed"] is False
    assert "stable_group_ids_not_observed" in payload["automation_blocks"]


def test_pipeimob_org_diagnostic_rejects_period_over_one_year():
    app.dependency_overrides[require_vista_diagnostic_admin] = lambda: "admin"
    try:
        response = TestClient(app).get(
            "/api/pipeimob/diagnostics/organizational-coverage"
            "?data_inicio=2025-01-01&data_fim=2026-09-07"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400

