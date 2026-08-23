import json
import urllib.error
import urllib.parse
from datetime import date

import pytest

from services.sales_reconciliation import reconcile_sales
from services.vista_sales_client import VistaSalesAPIError, VistaSalesClient


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


def test_vista_client_requests_only_documented_non_personal_fields():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(
            {
                "1": {
                    "Codigo": "vista-1",
                    "CodigoImovel": "44357",
                    "Status": "Ganho",
                    "DataFinal": "2026-08-10",
                    "ValorNegocio": "4500000",
                    "NomeCliente": "must-not-be-exposed",
                },
                "total": 1,
                "paginas": 1,
            }
        )

    client = VistaSalesClient(
        "https://tenant.example.com",
        "secret-key",
        "pipe-1",
        opener=opener,
    )
    gains = client.fetch_gains(date(2026, 8, 1), date(2026, 8, 20))

    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0].full_url).query)
    pesquisa = json.loads(query["pesquisa"][0])
    assert query["codigo_pipe"] == ["pipe-1"]
    assert pesquisa["filter"] == {
        "Status": "Ganho",
        "DataFinal": ["2026-08-01", "2026-08-20"],
    }
    assert "NomeCliente" not in pesquisa["fields"]
    assert "CodigoCorretor" not in pesquisa["fields"]
    assert "Corretor" not in pesquisa["fields"]
    assert "NomeCorretor" not in pesquisa["fields"]
    assert "CorretorNegocio" in pesquisa["fields"]
    assert "NomeCliente" not in gains[0]
    assert gains[0]["status"] == "Ganho"


def test_vista_client_resolves_commercial_broker_from_users_endpoint():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if "/negocios/listar?" in request.full_url:
            return FakeResponse(
                {
                    "1": {
                        "Codigo": "vista-1",
                        "CodigoImovel": "44357",
                        "Status": "Ganho",
                        "DataFinal": "2026-08-10",
                        "ValorNegocio": "4500000",
                        "CorretorNegocio": "77",
                    },
                    "total": 1,
                    "paginas": 1,
                }
            )
        return FakeResponse(
            {
                "77": {"Codigo": "77", "Nome": "Corretor Comercial"},
                "total": 1,
                "paginas": 1,
            }
        )

    client = VistaSalesClient(
        "https://tenant.example.com", "secret-key", "pipe-1", opener=opener
    )
    gains = client.fetch_gains(date(2026, 8, 1), date(2026, 8, 20))

    assert len(requests) == 2
    assert "/usuarios/listar?" in requests[1].full_url
    assert gains[0]["commercial_broker_id"] == "77"
    assert gains[0]["commercial_broker_name"] == "Corretor Comercial"


def test_vista_client_error_never_contains_api_key():
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "denied", None, None)

    client = VistaSalesClient(
        "https://tenant.example.com",
        "never-expose-me",
        "pipe-1",
        opener=opener,
    )

    with pytest.raises(VistaSalesAPIError) as error:
        client.fetch_gains(date(2026, 8, 1), date(2026, 8, 20))

    assert "never-expose-me" not in str(error.value)


def test_august_reconciliation_reproduces_validated_official_totals():
    pipe_values = {
        "31748": "450000",
        "42230": "1800000",
        "44246": "820000",
        "25024": "300000",
        "41821": "2050000",
        "42691": "807500",
        "42567": "1325000",
        "43964": "258000",
        "38678": "2500000",
        "44285": "290000",
        "40458": "800000",
        "43991": "725004",
        "44309": "1286000",
        "44258": "605000",
        "44357": "4500000",
        "44358": "4500000",
        "44485": "387000",
        "44495": "400000",
        "44555": "393000",
    }
    matched_codes = {"44258", "44357", "44358", "44485", "44495", "44555"}
    pipe_rows = [
        {
            "transacao_unique_id_pipeimob": f"pipe-{code}",
            "codigo_imovel": code,
            "data_assinatura_ccv": "2026-08-10",
            "valor_contrato": value,
            "total_comissao": "1000",
            "agente_gestor": "Gerente",
        }
        for code, value in pipe_values.items()
    ]
    vista_rows = [
        {
            "deal_id": f"vista-{code}",
            "property_code": code,
            "gain_date": "2026-08-10",
            "deal_value": pipe_values[code],
            "stage_name": "Fechamento",
            "commercial_broker_name": "Comercial",
        }
        for code in matched_codes
    ]

    result = reconcile_sales(pipe_rows, vista_rows)

    assert result["summary"]["official_sales"] == 19
    assert result["summary"]["official_vgv"] == "24196504"
    assert result["summary"]["official_vgc"] == "19000"
    assert result["summary"]["matched"] == 6
    assert result["summary"]["pipeimob_without_vista_gain"] == 13
    assert result["summary"]["vista_without_pipeimob_contract"] == 0


def test_closing_stage_is_not_treated_as_sale_without_vista_gain_status():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_assinatura_ccv": "2026-08-10",
                "valor_contrato": "100000",
            }
        ],
        [],
    )

    assert result["items"][0]["status"] == "PIPEIMOB_SEM_GANHO_VISTA"


def test_pipeimob_contract_date_is_used_when_ccv_signature_field_is_absent():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_contrato": "2026-08-10",
                "data_inicio_venda": "2026-07-01",
                "valor_contrato": "100000",
            }
        ],
        [],
    )

    assert result["summary"]["source_data_incomplete"] == 0
    assert result["items"][0]["official_sale_date"] == "2026-08-10"


def test_pipeimob_report_fields_are_preserved_for_director_portal():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_contrato": "2026-08-10",
                "data_inicio_venda": "2026-08-03",
                "valor_contrato": "100000",
                "total_comissao": "5530.20",
                "data_recebimento_comissao": "2026-08-12",
                "agente_gestor": "Gerente da Operação",
                "endereco_logradouro": "Rua Exemplo",
                "endereco_numero": "10",
                "endereco_complemento": "Apto 20",
                "endereco_bairro": "Centro",
                "endereco_cidade": "Florianópolis",
                "endereco_uf": "SC",
                "endereco_cep": "88000-000",
            }
        ],
        [],
    )

    item = result["items"][0]
    assert result["contract_version"] == "1.1"
    assert result["summary"]["official_vgc"] == "5530.20"
    assert item["ccv_signature_date"] == "2026-08-10"
    assert item["ccv_upload_date"] == "2026-08-03"
    assert item["commission_value"] == "5530.20"
    assert item["commission_date"] == "2026-08-12"
    assert item["pipeimob_manager"] == "Gerente da Operação"
    assert item["property_address"] == (
        "Rua Exemplo, 10, Apto 20, Centro, Florianópolis, SC, CEP 88000-000"
    )


def test_unique_property_match_enriches_broker_when_vista_omits_date_and_value():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_contrato": "2026-08-10",
                "valor_contrato": "100000",
            }
        ],
        [
            {
                "deal_id": "vista-1",
                "property_code": "100",
                "gain_date": None,
                "deal_value": None,
                "commercial_broker_name": "Corretor Comercial",
            }
        ],
    )

    assert result["summary"]["matched"] == 1
    assert result["summary"]["no_automatic_link"] == 0
    assert result["summary"]["vista_without_pipeimob_contract"] == 0
    assert result["items"][0]["status"] == "CONCILIADO"
    assert result["items"][0]["commercial_broker"] == "Corretor Comercial"
    assert result["items"][0]["official_sale_date"] == "2026-08-10"
    assert result["items"][0]["official_value"] == "100000"
    assert result["items"][0]["vista_gain_date"] is None
    assert result["items"][0]["vista_value"] is None


def test_incomplete_vista_fields_do_not_guess_between_duplicate_property_matches():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_contrato": "2026-08-10",
                "valor_contrato": "100000",
            }
        ],
        [
            {"deal_id": "vista-1", "property_code": "100"},
            {"deal_id": "vista-2", "property_code": "100"},
        ],
    )

    assert result["summary"]["matched"] == 0
    assert result["summary"]["no_automatic_link"] == 1
    assert result["summary"]["vista_without_pipeimob_contract"] == 2


def test_vista_client_rejects_closing_stage_even_if_api_ignores_gain_filter():
    def opener(request, timeout):
        return FakeResponse(
            {
                "1": {
                    "Codigo": "vista-fechamento",
                    "CodigoImovel": "100",
                    "Status": "Aberto",
                    "NomeEtapa": "Fechamento",
                    "DataFinal": "2026-08-10",
                    "ValorNegocio": "100000",
                },
                "2": {
                    "Codigo": "vista-ganho",
                    "CodigoImovel": "101",
                    "Status": "Ganho",
                    "NomeEtapa": "Fechamento",
                    "DataFinal": "2026-08-11",
                    "ValorNegocio": "200000",
                },
                "total": 2,
                "paginas": 1,
            }
        )

    client = VistaSalesClient(
        "https://tenant.example.com", "secret-key", "pipe-1", opener=opener
    )

    gains = client.fetch_gains(date(2026, 8, 1), date(2026, 8, 20))

    assert [gain["deal_id"] for gain in gains] == ["vista-ganho"]


def test_value_and_date_mismatches_remain_auditable():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_assinatura_ccv": "2026-08-01",
                "valor_contrato": "100000",
            }
        ],
        [
            {
                "deal_id": "vista-1",
                "property_code": "100",
                "gain_date": "2026-08-12",
                "deal_value": "99000",
            }
        ],
    )

    assert result["items"][0]["status"] == "DIVERGENCIA_VALOR"
    assert result["items"][0]["issues"] == [
        "DIVERGENCIA_VALOR",
        "DIVERGENCIA_DATA",
    ]


def test_vista_client_structured_failure_logging_redacts_sensitive_payloads(caplog):
    import logging
    def opener(request, timeout):
        fp = io.BytesIO(b'{"error": "failed", "key": "secret-token-123", "token": "jwt-abc"}')
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {"content-type": "application/json"}, fp)

    import io
    client = VistaSalesClient(
        "https://tenant.example.com",
        "secret-api-key-xyz",
        "pipe-1",
        opener=opener,
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(VistaSalesAPIError):
            client.fetch_gains(date(2026, 8, 1), date(2026, 8, 20))

    assert "secret-api-key-xyz" not in caplog.text
    assert "secret-token-123" not in caplog.text
    assert "jwt-abc" not in caplog.text
    assert "vista_sales_api_failed" in caplog.text
    assert "[REDACTED]" in caplog.text
