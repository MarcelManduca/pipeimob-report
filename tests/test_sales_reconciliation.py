import json
import urllib.error
import urllib.parse
from datetime import date

import pytest

from services.sales_reconciliation import rank_commercial_sales, reconcile_sales
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


def test_vista_client_can_request_a_tenant_confirmed_team_field():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(
            {
                "1": {
                    "Codigo": "vista-1",
                    "CodigoImovel": "44357",
                    "Status": "Ganho",
                    "EquipeNegocio": {"Nome": "Equipe Evolução"},
                },
                "total": 1,
                "paginas": 1,
            }
        )

    client = VistaSalesClient(
        "https://tenant.example.com",
        "secret-key",
        "pipe-1",
        team_field="EquipeNegocio",
        opener=opener,
    )
    gains = client.fetch_gains(date(2026, 8, 1), date(2026, 8, 20))

    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0].full_url).query)
    pesquisa = json.loads(query["pesquisa"][0])
    assert "EquipeNegocio" in pesquisa["fields"]
    assert gains[0]["commercial_team_name"] == "Equipe Evolução"


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
            "agente_gestor": "Fiscal",
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


def test_pipeimob_official_group_resolves_team_without_spreadsheet_sale_match():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_contrato": "2026-08-10",
                "valor_contrato": "100000",
                "agente_gestor": "Gerente Pipe",
                "agente_gestor_grupos_a_que_pertence": ["branch-1", "team-1"],
            }
        ],
        [],
        pipeimob_group_mapping={
            "branch-1": {"name": "Florianópolis", "type": "branch"},
            "team-1": {"name": "Equipe Meta", "type": "team"},
        },
    )

    item = result["items"][0]
    assert item["responsible_manager"] == "Gerente Pipe"
    assert item["team_name"] == "Equipe Meta"
    assert item["team_source"] == "pipeimob_responsible_group"
    assert item["team_resolution_status"] == "resolved"
    assert result["summary"]["api_team_resolved"] == 1


def test_multiple_pipeimob_team_groups_remain_ambiguous():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_contrato": "2026-08-10",
                "valor_contrato": "100000",
                "agente_gestor_grupos_a_que_pertence": ["team-1", "team-2"],
            }
        ],
        [],
        pipeimob_group_mapping={
            "team-1": {"name": "Equipe Meta", "type": "team"},
            "team-2": {"name": "Equipe Evolução", "type": "team"},
        },
    )

    item = result["items"][0]
    assert item["team_name"] is None
    assert item["team_resolution_status"] == "ambiguous_pipeimob_groups"
    assert result["summary"]["ambiguous_pipeimob_team"] == 1


def test_vista_deal_team_has_priority_and_api_conflict_is_auditable():
    result = reconcile_sales(
        [
            {
                "transacao_unique_id_pipeimob": "pipe-1",
                "codigo_imovel": "100",
                "data_contrato": "2026-08-10",
                "valor_contrato": "100000",
                "agente_gestor_grupos_a_que_pertence": ["team-1"],
            }
        ],
        [
            {
                "deal_id": "vista-1",
                "property_code": "100",
                "commercial_team_name": "Equipe Evolução",
            }
        ],
        pipeimob_group_mapping={
            "team-1": {"name": "Equipe Meta", "type": "team"},
        },
    )

    item = result["items"][0]
    assert item["team_name"] == "Equipe Evolução"
    assert item["team_source"] == "vista_deal"
    assert item["vista_team"] == "Equipe Evolução"
    assert item["pipeimob_team"] == "Equipe Meta"
    assert item["team_resolution_status"] == "conflict_api_sources"
    assert result["summary"]["api_team_conflicts"] == 1


def test_ranking_uses_official_sales_and_vista_commercial_broker():
    reconciliation = {
        "official_source": "pipeimob_api_v2",
        "commercial_source": "vista_negocio_ganho",
        "period": {"start": "2026-08-01", "end": "2026-08-31"},
        "summary": {"official_sales": 4, "official_vgv": "1000000"},
        "items": [
            {
                "pipeimob_transaction_id": "pipe-1",
                "vista_deal_id": "vista-1",
                "commercial_broker": "Ana Corretora",
                "official_value": "200000",
                "status": "CONCILIADO",
            },
            {
                "pipeimob_transaction_id": "pipe-2",
                "vista_deal_id": "vista-2",
                "commercial_broker": " ana   corretora ",
                "official_value": "300000",
                "status": "DIVERGENCIA_VALOR",
            },
            {
                "pipeimob_transaction_id": "pipe-3",
                "vista_deal_id": "vista-3",
                "commercial_broker": "Bruno Corretor",
                "official_value": "400000",
                "status": "CONCILIADO",
            },
            {
                "pipeimob_transaction_id": "pipe-4",
                "vista_deal_id": None,
                "commercial_broker": None,
                "official_value": "100000",
                "status": "PIPEIMOB_SEM_GANHO_VISTA",
            },
            {
                "pipeimob_transaction_id": None,
                "vista_deal_id": "vista-only",
                "commercial_broker": "Bruno Corretor",
                "official_value": None,
                "status": "VISTA_SEM_CONTRATO_PIPEIMOB",
            },
        ],
    }

    result = rank_commercial_sales(reconciliation)

    assert result["attribution"] == "vista_commercial_broker"
    assert result["ranking"][0] == {
        "commercial_broker": "Ana Corretora",
        "sales_count": 2,
        "vgv": "500000",
        "average_ticket": "250000",
        "position": 1,
    }
    assert result["ranking"][1]["commercial_broker"] == "Bruno Corretor"
    assert result["summary"] == {
        "official_sales": 4,
        "official_vgv": "1000000",
        "attributed_sales": 3,
        "attributed_vgv": "900000",
        "unattributed_sales": 1,
        "unattributed_vgv": "100000",
    }


def test_ranking_can_order_by_vgv():
    reconciliation = {
        "summary": {"official_sales": 3, "official_vgv": "900000"},
        "items": [
            {
                "pipeimob_transaction_id": "1",
                "commercial_broker": "Mais contratos",
                "official_value": "100000",
            },
            {
                "pipeimob_transaction_id": "2",
                "commercial_broker": "Mais contratos",
                "official_value": "100000",
            },
            {
                "pipeimob_transaction_id": "3",
                "commercial_broker": "Maior VGV",
                "official_value": "700000",
            },
        ],
    }

    result = rank_commercial_sales(reconciliation, metric="vgv")

    assert result["ranking"][0]["commercial_broker"] == "Maior VGV"
    assert result["ranking"][0]["position"] == 1


def test_build_funnel_payload_non_auditable_gain_dates_behavior():
    from services.sales_reconciliation import build_funnel_payload

    rec_data = {
        "summary": {
            "official_sales": 2,
            "official_vgv": "600000.00",
            "matched": 1,
            "pipeimob_without_vista_gain": 1,
            "vista_without_pipeimob_contract": 1,
            "api_team_resolved": 0,
            "api_team_unresolved": 2,
        },
        "items": [
            # 1. Matched with null gain date
            {
                "status": "CONCILIADO",
                "pipeimob_transaction_id": "p-1",
                "vista_deal_id": "v-1",
                "vista_gain_date": None,
            },
            # 2. Vista without CCV with null gain date
            {
                "status": "VISTA_SEM_CONTRATO_PIPEIMOB",
                "pipeimob_transaction_id": None,
                "vista_deal_id": "v-2",
                "vista_gain_date": None,
            },
            # 3. Pipeimob without Vista (must NOT increase non-auditable count)
            {
                "status": "PIPEIMOB_SEM_GANHO_VISTA",
                "pipeimob_transaction_id": "p-2",
                "vista_deal_id": None,
                "vista_gain_date": None,
            },
            # 4. Duplicate entry for v-1 (must NOT double-count)
            {
                "status": "CONCILIADO",
                "pipeimob_transaction_id": "p-1-dup",
                "vista_deal_id": "v-1",
                "vista_gain_date": None,
            },
            # 5. Matched with valid audit date
            {
                "status": "CONCILIADO",
                "pipeimob_transaction_id": "p-3",
                "vista_deal_id": "v-3",
                "vista_gain_date": "2026-08-15",
            },
        ],
    }

    payload = build_funnel_payload(
        start_date="2026-08-01",
        end_date="2026-08-31",
        official_transactions=[{"id": "p-1"}, {"id": "p-2"}],
        official_vgv="600000.00",
        official_vgc="24000.00",
        vista_funnel_data=None,
        reconciliation_data=rec_data,
    )

    rec = payload["reconciliation"]
    # Distinct non-auditable deals: only v-1 and v-2
    assert rec["non_auditable_gain_dates_count"] == 2
    assert rec["unresolved_gain_dates"] == 2
    assert rec["official_sales_count"] == 2
    assert rec["matched_count"] == 1
    assert rec["vista_without_ccv_count"] == 1
    assert rec["ccv_without_vista_count"] == 1
    assert rec["divergence_flag"] is True


def test_build_funnel_payload_vgc_summation_without_fixed_rate():
    from services.sales_reconciliation import build_funnel_payload

    # Synthetic transactions with varying commission percentages:
    # Tx1: 100k @ 3% = 3k
    # Tx2: 500k @ 6% = 30k
    # Tx3: 200k @ 4.5% = 9k
    # Sum: VGV = 800k, VGC = 42k (effective rate = 5.25%, not 5.0% or 4.0%)
    transactions = [
        {"transacao_unique_id_pipeimob": "tx-1", "valor_contrato": 100000, "total_comissao": 3000},
        {"transacao_unique_id_pipeimob": "tx-2", "valor_contrato": 500000, "total_comissao": 30000},
        {"transacao_unique_id_pipeimob": "tx-3", "valor_contrato": 200000, "total_comissao": 9000},
    ]

    payload = build_funnel_payload(
        start_date="2026-08-01",
        end_date="2026-08-31",
        official_transactions=transactions,
        official_vgv=None,
        official_vgc=None,
        vista_funnel_data=None,
        reconciliation_data=None,
    )

    assert payload["official_vgv"] == "800000.00"
    assert payload["official_vgc"] == "42000.00"
    assert payload["official_vgc_details"]["amount"] == "42000.00"
    assert payload["official_vgc_details"]["source_field"] == "total_comissao"
    assert payload["official_vgc_details"]["availability"] == "available"
    assert payload["official_vgc_details"]["transactions_total"] == 3
    assert payload["official_vgc_details"]["transactions_with_commission"] == 3
    assert payload["official_vgc_details"]["transactions_missing_commission"] == 0
    # Ensure it did not apply 5.0% (which would be 40000.00) or 4.0% (which would be 32000.00)
    assert payload["official_vgc"] != "40000.00"
    assert payload["official_vgc"] != "32000.00"


def test_decimal_precision_immunity_to_float_binary_errors():
    """Prove Decimal avoids binary floating point summation errors (e.g. 100.05 + 200.05 + 300.05)."""
    from decimal import Decimal
    from services.sales_reconciliation import build_funnel_payload, _parse_decimal

    # Python float summation produces: 100.05 + 200.05 + 300.05 == 600.1500000000001
    float_sum = 100.05 + 200.05 + 300.05
    assert float_sum != 600.15  # Shows standard float imprecision

    transactions = [
        {"transacao_unique_id_pipeimob": "tx-f1", "valor_contrato": "100.05", "total_comissao": "5.05"},
        {"transacao_unique_id_pipeimob": "tx-f2", "valor_contrato": "200.05", "total_comissao": "10.05"},
        {"transacao_unique_id_pipeimob": "tx-f3", "valor_contrato": "300.05", "total_comissao": "15.05"},
    ]

    payload = build_funnel_payload(
        start_date="2026-08-01",
        end_date="2026-08-31",
        official_transactions=transactions,
        official_vgv=None,
        official_vgc=None,
    )

    # Exact Decimal sum: 600.15 and 30.15
    assert payload["official_vgv"] == "600.15"
    assert payload["official_vgc"] == "30.15"
    assert payload["official_vgc_details"]["amount"] == "30.15"


def test_vgc_completeness_states_available_partial_unavailable():
    """Validate full, partial, and unavailable VGC contract states and zero-presumption behavior."""
    from services.sales_reconciliation import build_funnel_payload

    # Scenario 1: Partial commission availability (2 of 3 contracts have commission)
    partial_txs = [
        {"transacao_unique_id_pipeimob": "tx-p1", "valor_contrato": 100000, "total_comissao": 5000},
        {"transacao_unique_id_pipeimob": "tx-p2", "valor_contrato": 200000, "total_comissao": 10000},
        {"transacao_unique_id_pipeimob": "tx-p3", "valor_contrato": 300000, "total_comissao": None},  # Missing
    ]
    partial_payload = build_funnel_payload(
        start_date="2026-08-01",
        end_date="2026-08-31",
        official_transactions=partial_txs,
        official_vgv=None,
        official_vgc=None,
    )
    p_det = partial_payload["official_vgc_details"]
    assert p_det["availability"] == "partial"
    assert p_det["amount"] == "15000.00"  # Sum of only observed commissions
    assert p_det["transactions_total"] == 3
    assert p_det["transactions_with_commission"] == 2
    assert p_det["transactions_missing_commission"] == 1
    assert "2 de 3" in p_det["reason"]
    # Ensure missing contract was NOT inferred (if inferred at 5%, sum would be 30000)
    assert p_det["amount"] != "30000.00"

    # Scenario 2: Unavailable commission (0 of 3 contracts have commission)
    unavail_txs = [
        {"transacao_unique_id_pipeimob": "tx-u1", "valor_contrato": 100000, "total_comissao": None},
        {"transacao_unique_id_pipeimob": "tx-u2", "valor_contrato": 200000, "total_comissao": None},
    ]
    unavail_payload = build_funnel_payload(
        start_date="2026-08-01",
        end_date="2026-08-31",
        official_transactions=unavail_txs,
        official_vgv=None,
        official_vgc=None,
    )
    u_det = unavail_payload["official_vgc_details"]
    assert u_det["availability"] == "unavailable"
    assert u_det["amount"] is None
    assert u_det["transactions_total"] == 2
    assert u_det["transactions_with_commission"] == 0
    assert u_det["transactions_missing_commission"] == 2
    assert u_det["reason"] is not None


def test_reconciliation_exact_mathematical_partition_and_discrepancy_proof():
    """Verify that all 310 official sales and 327 Vista gains form exact mathematical identities.

    Proves why the dashboard previously displayed 306 (276 + 30) instead of 327 (297 + 30)
    due to omitting the 21 linked deals with value (18) and date (3) divergences.
    """
    pipe_txs = []
    # 276 strictly matched deals (same property, date within 7 days, same value)
    for i in range(1, 277):
        pipe_txs.append({
            "transacao_unique_id_pipeimob": f"tx-match-{i}",
            "codigo_imovel": f"PROP-{i}",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "500000.00",
        })

    # 18 value mismatches (same property, date match, different value)
    for i in range(1, 19):
        pipe_txs.append({
            "transacao_unique_id_pipeimob": f"tx-val-{i}",
            "codigo_imovel": f"PROP-VAL-{i}",
            "data_assinatura_ccv": "2026-05-15",
            "valor_contrato": "500000.00",
        })

    # 3 date mismatches (same property, delay > 7 days, same value)
    for i in range(1, 4):
        pipe_txs.append({
            "transacao_unique_id_pipeimob": f"tx-date-{i}",
            "codigo_imovel": f"PROP-DATE-{i}",
            "data_assinatura_ccv": "2026-05-01",
            "valor_contrato": "500000.00",
        })

    # 13 CCV without Vista gain (unlinked Pipeimob contracts)
    for i in range(1, 14):
        pipe_txs.append({
            "transacao_unique_id_pipeimob": f"tx-nogain-{i}",
            "codigo_imovel": f"PROP-NOGAIN-{i}",
            "data_assinatura_ccv": "2026-05-20",
            "valor_contrato": "500000.00",
        })

    assert len(pipe_txs) == 310

    vista_gains = []
    # 276 matching Vista gains
    for i in range(1, 277):
        vista_gains.append({
            "deal_id": f"deal-match-{i}",
            "property_code": f"PROP-{i}",
            "gain_date": "2026-05-10",
            "deal_value": "500000.00",
            "commercial_broker_name": "Corretor Conciliado",
        })

    # 18 value mismatch Vista gains (different value, e.g. 550,000)
    for i in range(1, 19):
        vista_gains.append({
            "deal_id": f"deal-val-{i}",
            "property_code": f"PROP-VAL-{i}",
            "gain_date": "2026-05-15",
            "deal_value": "550000.00",
            "commercial_broker_name": "Corretor Valor",
        })

    # 3 date mismatch Vista gains (e.g. gain_date 2026-05-25, 24 days later)
    for i in range(1, 4):
        vista_gains.append({
            "deal_id": f"deal-date-{i}",
            "property_code": f"PROP-DATE-{i}",
            "gain_date": "2026-05-25",
            "deal_value": "500000.00",
            "commercial_broker_name": "Corretor Data",
        })

    # 30 Vista gains without CCV contract
    for i in range(1, 31):
        vista_gains.append({
            "deal_id": f"deal-noccv-{i}",
            "property_code": f"PROP-NOCCV-{i}",
            "gain_date": "2026-05-18",
            "deal_value": "600000.00",
            "commercial_broker_name": "Corretor Sem CCV",
        })

    assert len(vista_gains) == 327

    res = reconcile_sales(pipe_txs, vista_gains, date_tolerance_days=7)
    s = res["summary"]

    # 1. Total Official Sales = 310
    assert s["official_sales"] == 310

    # 2. Total Linked Transactions = 297 (276 + 18 + 3)
    assert s["total_linked"] == 297
    assert s["matched"] == 276
    assert s["strictly_matched"] == 276
    assert s["divergent_matches"] == 21
    assert s["value_mismatches"] == 18
    assert s["date_mismatches"] == 3

    # 3. Unlinked categories
    assert s["pipeimob_without_vista_gain"] == 13
    assert s["vista_without_pipeimob_contract"] == 30
    assert s["total_vista_gains"] == 327
    assert s["vista_gains"] == 327

    # 4. Rigorous Mathematical Identities
    # Identity A: official_sales = matched + divergent_matches + pipeimob_without_vista_gain
    assert s["matched"] + s["divergent_matches"] + s["pipeimob_without_vista_gain"] == s["official_sales"]
    assert s["total_linked"] + s["pipeimob_without_vista_gain"] == s["official_sales"]
    assert 297 + 13 == 310

    # Identity B: total_vista_gains = total_linked + vista_without_pipeimob_contract
    assert s["matched"] + s["divergent_matches"] + s["vista_without_pipeimob_contract"] == s["total_vista_gains"]
    assert s["total_linked"] + s["vista_without_pipeimob_contract"] == s["total_vista_gains"]
    assert 297 + 30 == 327

    # Identity C: The 306 discrepancy explanation
    # Old frontend formula: matched (276) + vista_without_ccv (30) = 306 (missing 21 divergent deals)
    assert s["matched"] + s["vista_without_pipeimob_contract"] == 306
    assert (s["matched"] + s["vista_without_pipeimob_contract"]) + s["divergent_matches"] == 327


def test_reconciliation_prohibits_ultima_atualizacao_as_gain_date():
    """Verify that UltimaAtualizacao is never parsed or accepted as gain_date and DataFinal=null remains null."""
    pipe_txs = [
        {
            "transacao_unique_id_pipeimob": "tx-1",
            "codigo_imovel": "PROP-1",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "500000.00",
        }
    ]
    # Gain with null DataFinal and existing UltimaAtualizacao
    vista_gains = [
        {
            "deal_id": "deal-1",
            "property_code": "PROP-1",
            "gain_date": None,  # DataFinal was null
            "UltimaAtualizacao": "2026-05-10",  # Must be ignored as gain date
            "deal_value": "500000.00",
        }
    ]

    res = reconcile_sales(pipe_txs, vista_gains)
    s = res["summary"]
    items = res["items"]

    assert items[0]["vista_gain_date"] is None
    assert s["unresolved_gain_dates"] == 1


