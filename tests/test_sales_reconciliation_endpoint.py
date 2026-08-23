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
            "total_comissao": "6000",
            "endereco_logradouro": "Rua Exemplo",
            "endereco_numero": "10",
            "agente_gestor": "Gerente Pipeimob",
        }
    ]

    try:
        with patch(
            "main.load_transactions_dataset",
            return_value=("live", "pipeimob_api_v2", pipe_rows, 1, "fresh"),
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
    assert data["summary"]["official_vgc"] == "6000"
    assert data["items"][0]["pipeimob_manager"] == "Gerente Pipeimob"
    assert data["items"][0]["fiscal_broker"] == "Gerente Pipeimob"
    assert data["items"][0]["property_address"] == "Rua Exemplo, 10"
    assert data["items"][0]["broker_roles_differ"] is True
    assert response.headers["X-Reconciliation-Contract"] == "1.1"
