from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app, verify_backend_api_key


class FakeVistaClient:
    def fetch_gains(self, start_date, end_date):
        return [
            {
                "deal_id": "vista-1",
                "property_code": "100",
                "gain_date": "2026-08-10",
                "deal_value": "100000",
                "stage_name": "Fechamento",
                "commercial_broker_name": "Corretor Comercial",
            }
        ]


def test_reconciliation_endpoint_uses_live_pipeimob_and_vista_sources():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    pipe_rows = [
        {
            "transacao_unique_id_pipeimob": "pipe-1",
            "codigo_imovel": "100",
            "codigo_contrato": "contract-1",
            "data_contrato": "2026-08-10",
            "data_inicio_venda": "2026-07-01",
            "valor_contrato": "100000",
            "agente_gestor": "Agente Fiscal",
            "agente_gestor_grupos_a_que_pertence": ["team-1"],
        }
    ]

    try:
        with patch(
            "main.load_transactions_dataset",
            return_value=("live", "pipeimob_api_v2", pipe_rows, 1, "fresh"),
        ), patch(
            "main.parse_official_team_groups",
            return_value=(
                "configured",
                True,
                {"team-1": {"name": "Equipe Meta", "type": "team"}},
                ["Equipe Meta"],
            ),
        ), patch("main.VistaSalesClient.from_env", return_value=FakeVistaClient()):
            response = TestClient(app).get(
                "/api/reconciliation/sales"
                "?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-20"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["official_sales"] == 1
    assert data["summary"]["matched"] == 1
    assert data["items"][0]["commercial_broker"] == "Corretor Comercial"
    assert data["items"][0]["fiscal_broker"] == "Agente Fiscal"
    assert data["items"][0]["responsible_manager"] == "Agente Fiscal"
    assert data["items"][0]["team_name"] == "Equipe Meta"
    assert data["items"][0]["team_source"] == "pipeimob_responsible_group"
    assert data["items"][0]["broker_roles_differ"] is True
    assert response.headers["X-Reconciliation-Contract"] == "1.1"


def test_ranking_endpoint_returns_aggregates_without_transaction_ids():
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}
    pipe_rows = [
        {
            "transacao_unique_id_pipeimob": "pipe-1",
            "codigo_imovel": "100",
            "codigo_contrato": "contract-1",
            "data_contrato": "2026-08-10",
            "valor_contrato": "100000",
            "agente_gestor": "Agente Fiscal",
        }
    ]

    try:
        with patch(
            "main.load_transactions_dataset",
            return_value=("live", "pipeimob_api_v2", pipe_rows, 1, "fresh"),
        ), patch("main.VistaSalesClient.from_env", return_value=FakeVistaClient()):
            response = TestClient(app).get(
                "/api/reconciliation/sales/ranking"
                "?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-20"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["ranking"] == [
        {
            "commercial_broker": "Corretor Comercial",
            "sales_count": 1,
            "vgv": "100000",
            "average_ticket": "100000",
            "position": 1,
        }
    ]
    assert "pipeimob_transaction_id" not in response.text
    assert response.headers["X-Sales-Attribution"] == "vista_commercial_broker"


def test_reconciliation_multi_period_isolation_and_dynamic_calculation():
    """Verify that distinct query periods produce strictly distinct dynamic metrics without cache pollution or fixture reuse."""
    app.dependency_overrides[verify_backend_api_key] = lambda: {"sub": "test"}

    # Diverse dataset across multiple months: Jan, Mar, Aug, Sept
    pipe_rows = [
        # Jan sale
        {
            "transacao_unique_id_pipeimob": "pipe-jan-1",
            "codigo_imovel": "PROP-JAN",
            "codigo_contrato": "C-JAN",
            "data_contrato": "2026-01-15",
            "valor_contrato": "500000.00",
            "agente_gestor": "Corretor Jan",
        },
        # March sale (value mismatch in Vista)
        {
            "transacao_unique_id_pipeimob": "pipe-mar-1",
            "codigo_imovel": "PROP-MAR",
            "codigo_contrato": "C-MAR",
            "data_contrato": "2026-03-20",
            "valor_contrato": "600000.00",
            "agente_gestor": "Corretor Mar",
        },
        # August sale 1 (strictly matched with valid DataFinal in Vista)
        {
            "transacao_unique_id_pipeimob": "pipe-aug-1",
            "codigo_imovel": "PROP-AUG-1",
            "codigo_contrato": "C-AUG-1",
            "data_contrato": "2026-08-10",
            "valor_contrato": "800000.00",
            "agente_gestor": "Corretor Aug 1",
        },
        # August sale 2 (null DataFinal in Vista)
        {
            "transacao_unique_id_pipeimob": "pipe-aug-2",
            "codigo_imovel": "PROP-AUG-2",
            "codigo_contrato": "C-AUG-2",
            "data_contrato": "2026-08-25",
            "valor_contrato": "450000.00",
            "agente_gestor": "Corretor Aug 2",
        },
    ]

    class MultiPeriodVistaClient:
        def fetch_gains(self, start_date, end_date):
            all_gains = [
                # Jan gain (null DataFinal)
                {
                    "deal_id": "vista-jan-1",
                    "property_code": "PROP-JAN",
                    "gain_date": None,
                    "deal_value": "500000.00",
                },
                # Mar gain (value mismatch 650k vs 600k, null DataFinal)
                {
                    "deal_id": "vista-mar-1",
                    "property_code": "PROP-MAR",
                    "gain_date": None,
                    "deal_value": "650000.00",
                },
                # Aug gain 1 (valid DataFinal: 2026-08-12, within 7d tolerance)
                {
                    "deal_id": "vista-aug-1",
                    "property_code": "PROP-AUG-1",
                    "gain_date": "2026-08-12",
                    "deal_value": "800000.00",
                },
                # Aug gain 2 (null DataFinal)
                {
                    "deal_id": "vista-aug-2",
                    "property_code": "PROP-AUG-2",
                    "gain_date": None,
                    "deal_value": "450000.00",
                },
                # Aug gain without CCV
                {
                    "deal_id": "vista-aug-extra",
                    "property_code": "PROP-AUG-EXTRA",
                    "gain_date": "2026-08-18",
                    "deal_value": "300000.00",
                },
            ]
            return [
                g for g in all_gains
                if g["gain_date"] is None or (start_date <= date.fromisoformat(g["gain_date"]) <= end_date)
            ]

    from datetime import date
    try:
        with patch(
            "main.load_transactions_dataset",
            return_value=("live", "pipeimob_api_v2", pipe_rows, 1, "fresh"),
        ), patch("main.VistaSalesClient.from_env", return_value=MultiPeriodVistaClient()):
            client = TestClient(app)

            # Period 1: YTD (01/01/2026 to 09/09/2026)
            res_ytd = client.get(
                "/api/reconciliation/sales"
                "?data_inicio_ccv=2026-01-01&data_fim_ccv=2026-09-09&date_tolerance_days=7&refresh=true"
            )
            assert res_ytd.status_code == 200
            data_ytd = res_ytd.json()
            sum_ytd = data_ytd["summary"]

            # Period 2: August (01/08/2026 to 31/08/2026)
            res_aug = client.get(
                "/api/reconciliation/sales"
                "?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31&date_tolerance_days=7&refresh=true"
            )
            assert res_aug.status_code == 200
            data_aug = res_aug.json()
            sum_aug = data_aug["summary"]
    finally:
        app.dependency_overrides.clear()

    # 1. YTD assertions: all 4 pipe transactions included
    assert sum_ytd["official_sales"] == 4
    assert sum_ytd["total_linked_unique"] == 4
    assert sum_ytd["confirmed_value_mismatches"] == 1  # Mar deal
    assert sum_ytd["confirmed_divergent_linked_unique"] == 1
    assert sum_ytd["fully_audited_match"] == 1  # Aug 1 has valid DataFinal
    assert sum_ytd["value_matched_date_unresolved"] == 2  # Jan and Aug 2
    assert sum_ytd["non_auditable_gain_dates"] == 3  # Jan, Mar, Aug 2 have None
    assert sum_ytd["total_vista_gains"] == 5

    # 2. August assertions: strictly 2 pipe transactions in August
    assert sum_aug["official_sales"] == 2
    assert sum_aug["total_linked_unique"] == 2
    assert sum_aug["confirmed_value_mismatches"] == 0
    assert sum_aug["confirmed_divergent_linked_unique"] == 0
    assert sum_aug["fully_audited_match"] == 1  # Aug 1
    assert sum_aug["value_matched_date_unresolved"] == 1  # Aug 2
    assert sum_aug["non_auditable_gain_dates"] == 1  # Only Aug 2 in this period
    assert sum_aug["total_vista_gains"] == 3
    assert sum_aug["vista_without_pipeimob_contract"] == 1  # Aug extra

    # 3. Dynamic contrast proof: metrics differ across periods
    assert sum_ytd["official_sales"] != sum_aug["official_sales"]
    assert sum_ytd["total_linked_unique"] != sum_aug["total_linked_unique"]
    assert sum_ytd["confirmed_value_mismatches"] != sum_aug["confirmed_value_mismatches"]

