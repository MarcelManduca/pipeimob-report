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

    assert result["summary"]["matched"] == 0
    assert result["summary"]["linked_with_unresolved_gain_date"] == 1
    assert result["summary"]["total_linked_unique"] == 1
    assert result["summary"]["no_automatic_link"] == 0
    assert result["summary"]["vista_without_pipeimob_contract"] == 0
    assert result["items"][0]["status"] == "VINCULADO_COM_DATA_GANHO_NAO_RESOLVIDA"
    assert result["items"][0]["commercial_broker"] == "Corretor Comercial"
    assert result["items"][0]["official_sale_date"] == "2026-08-10"
    assert result["items"][0]["official_value"] == "100000"
    assert result["items"][0]["vista_gain_date"] is None
    assert result["items"][0]["delay_days"] is None
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
    """Verify that all 310 official sales and 320 Vista gains form exact mathematical identities.

    Partitions:
    - 276 strictly matched deals (same property, auditable date within tolerance, same value)
    - 13 value-only mismatches (same property, different value)
    - 8 linked deals with unresolved gain date (same property, same value, DataFinal is null)
    - 13 CCV without Vista gain (unlinked Pipeimob contracts)
    - 23 Vista gains without CCV contract (unlinked Vista deals)
    Total official sales = 310 (276 + 13 + 8 + 13)
    Total linked unique = 297 (276 + 13 + 8)
    Total Vista gains = 320 (297 + 23)
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

    # 13 value mismatches (same property, different value)
    for i in range(1, 14):
        pipe_txs.append({
            "transacao_unique_id_pipeimob": f"tx-val-{i}",
            "codigo_imovel": f"PROP-VAL-{i}",
            "data_assinatura_ccv": "2026-05-15",
            "valor_contrato": "500000.00",
        })

    # 8 linked deals with unresolved gain date (DataFinal is null in Vista CRM, same value)
    for i in range(1, 9):
        pipe_txs.append({
            "transacao_unique_id_pipeimob": f"tx-unresolved-{i}",
            "codigo_imovel": f"PROP-UNRESOLVED-{i}",
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
    # 276 matching Vista gains (with valid auditable gain_date)
    for i in range(1, 277):
        vista_gains.append({
            "deal_id": f"deal-match-{i}",
            "property_code": f"PROP-{i}",
            "gain_date": "2026-05-10",
            "deal_value": "500000.00",
            "commercial_broker_name": "Corretor Conciliado",
        })

    # 13 value mismatch Vista gains (different value, e.g. 550,000)
    for i in range(1, 14):
        vista_gains.append({
            "deal_id": f"deal-val-{i}",
            "property_code": f"PROP-VAL-{i}",
            "gain_date": "2026-05-15",
            "deal_value": "550000.00",
            "commercial_broker_name": "Corretor Valor",
        })

    # 8 linked Vista gains with NULL DataFinal (gain_date is None)
    for i in range(1, 9):
        vista_gains.append({
            "deal_id": f"deal-unresolved-{i}",
            "property_code": f"PROP-UNRESOLVED-{i}",
            "gain_date": None,  # DataFinal is null in Vista CRM
            "deal_value": "500000.00",
            "commercial_broker_name": "Corretor Unresolved Date",
        })

    # 23 Vista gains without CCV contract
    for i in range(1, 24):
        vista_gains.append({
            "deal_id": f"deal-noccv-{i}",
            "property_code": f"PROP-NOCCV-{i}",
            "gain_date": "2026-05-18",
            "deal_value": "600000.00",
            "commercial_broker_name": "Corretor Sem CCV",
        })

    assert len(vista_gains) == 320

    res = reconcile_sales(pipe_txs, vista_gains, date_tolerance_days=7)
    s = res["summary"]

    # 1. Total Official Sales = 310
    assert s["official_sales"] == 310

    # 2. Total Linked Transactions = 297 (276 strictly matched + 13 value-only + 0 date-only + 8 unresolved date)
    assert s["total_linked"] == 297
    assert s["total_linked_unique"] == 297
    assert s["matched"] == 276
    assert s["strictly_matched"] == 276
    assert s["divergent_matches"] == 21
    assert s["divergent_linked_unique"] == 21
    assert s["value_mismatches"] == 13
    assert s["value_only_mismatches"] == 13
    assert s["date_mismatches"] == 0
    assert s["date_only_mismatches"] == 0
    assert s["value_and_date_mismatches"] == 0
    assert s["linked_with_unresolved_gain_date"] == 8
    assert (
        s["value_only_mismatches"]
        + s["date_only_mismatches"]
        + s["value_and_date_mismatches"]
        + s["linked_with_unresolved_gain_date"]
        == s["divergent_linked_unique"]
    )

    # 3. Unlinked categories
    assert s["pipeimob_without_vista_gain"] == 13
    assert s["vista_without_pipeimob_contract"] == 23
    assert s["total_vista_gains"] == 320
    assert s["vista_gains"] == 320
    assert s["non_auditable_gain_dates"] == 8
    assert s["gain_period_basis"] == "DataFinal_only_if_present_else_unresolved"
    assert "UltimaAtualizacao" in s["limitation_note"]

    # 4. Rigorous Mathematical Identities
    # Identity A: official_sales = strictly_matched + value_only + date_only + both + unresolved_date + pipeimob_without_vista_gain
    assert (
        s["strictly_matched"]
        + s["value_only_mismatches"]
        + s["date_only_mismatches"]
        + s["value_and_date_mismatches"]
        + s["linked_with_unresolved_gain_date"]
        + s["pipeimob_without_vista_gain"]
        == s["official_sales"]
    )
    assert s["strictly_matched"] + s["divergent_linked_unique"] + s["pipeimob_without_vista_gain"] == s["official_sales"]
    assert s["total_linked_unique"] + s["pipeimob_without_vista_gain"] == s["official_sales"]
    assert 276 + 21 + 13 == 310
    assert 297 + 13 == 310

    # Identity B: total_vista_gains = total_linked + vista_without_pipeimob_contract
    assert s["total_linked_unique"] + s["vista_without_pipeimob_contract"] == s["total_vista_gains"]
    assert 297 + 23 == 320

    # Items audit: verify that the 8 unresolved items have null delay_days and no DATE_MISMATCH issue
    unresolved_items = [
        item for item in res["items"]
        if item.get("property_code", "").startswith("PROP-UNRESOLVED-")
    ]
    assert len(unresolved_items) == 8
    for item in unresolved_items:
        assert item["vista_gain_date"] is None
        assert item["delay_days"] is None
        assert "DIVERGENCIA_DATA" not in item["issues"]
        assert "VINCULADO_COM_DATA_GANHO_NAO_RESOLVIDA" in item["issues"]


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
    assert s["non_auditable_gain_dates"] == 1
    assert s["gain_period_basis"] == "DataFinal_only_if_present_else_unresolved"
    assert "UltimaAtualizacao" in s["limitation_note"]


def test_reconciliation_simultaneous_value_and_date_mismatch_no_double_counting():
    """Verify that deals with simultaneous value and date mismatch are never double-counted."""
    pipe_txs = [
        # 1. Strictly matched
        {
            "transacao_unique_id_pipeimob": "tx-strict",
            "codigo_imovel": "PROP-STRICT",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "500000.00",
        },
        # 2. Value-only mismatch
        {
            "transacao_unique_id_pipeimob": "tx-val",
            "codigo_imovel": "PROP-VAL",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "500000.00",
        },
        # 3. Date-only mismatch (> 7 days)
        {
            "transacao_unique_id_pipeimob": "tx-date",
            "codigo_imovel": "PROP-DATE",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "500000.00",
        },
        # 4. Simultaneous value AND date mismatch
        {
            "transacao_unique_id_pipeimob": "tx-both",
            "codigo_imovel": "PROP-BOTH",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "500000.00",
        },
        # 5. Pipeimob without Vista gain
        {
            "transacao_unique_id_pipeimob": "tx-nocrm",
            "codigo_imovel": "PROP-NOCRM",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "500000.00",
        },
    ]

    vista_gains = [
        # 1. Strictly matched
        {
            "deal_id": "deal-strict",
            "property_code": "PROP-STRICT",
            "gain_date": "2026-05-10",
            "deal_value": "500000.00",
        },
        # 2. Value-only mismatch (diff value, same date)
        {
            "deal_id": "deal-val",
            "property_code": "PROP-VAL",
            "gain_date": "2026-05-10",
            "deal_value": "550000.00",
        },
        # 3. Date-only mismatch (diff date > 7d, same value)
        {
            "deal_id": "deal-date",
            "property_code": "PROP-DATE",
            "gain_date": "2026-05-30",
            "deal_value": "500000.00",
        },
        # 4. Simultaneous value AND date mismatch (diff date > 7d AND diff value)
        {
            "deal_id": "deal-both",
            "property_code": "PROP-BOTH",
            "gain_date": "2026-05-30",
            "deal_value": "600000.00",
        },
        # 6. Vista gain without Pipeimob contract
        {
            "deal_id": "deal-nopipe",
            "property_code": "PROP-NOPIPE",
            "gain_date": "2026-05-12",
            "deal_value": "400000.00",
        },
    ]

    res = reconcile_sales(pipe_txs, vista_gains, date_tolerance_days=7)
    s = res["summary"]

    assert s["official_sales"] == 5
    assert s["strictly_matched"] == 1
    assert s["value_only_mismatches"] == 1
    assert s["date_only_mismatches"] == 1
    assert s["value_and_date_mismatches"] == 1
    assert s["divergent_linked_unique"] == 3  # 1 val + 1 date + 1 both (never 4!)
    assert s["total_linked_unique"] == 4  # 1 strict + 3 divergent
    assert s["pipeimob_without_vista_gain"] == 1
    assert s["vista_without_pipeimob_contract"] == 1
    assert s["total_vista_gains"] == 5  # 4 linked + 1 unlinked

    # Mathematical identities
    assert s["strictly_matched"] + s["divergent_linked_unique"] + s["pipeimob_without_vista_gain"] == s["official_sales"]
    assert s["total_linked_unique"] + s["vista_without_pipeimob_contract"] == s["total_vista_gains"]


def test_reconciliation_null_datafinal_rules_and_delay_days():
    """Verify that null DataFinal in Vista produces delay_days=None, never produces DATE_MISMATCH, and uses linked_with_unresolved_gain_date."""
    pipe_txs = [
        # Deal with matching property and value, but Vista has DataFinal=null
        {
            "transacao_unique_id_pipeimob": "tx-null-df",
            "codigo_imovel": "PROP-NULL-DF",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "750000.00",
        },
        # Deal with matching property and value, and Vista has valid DataFinal outside tolerance
        {
            "transacao_unique_id_pipeimob": "tx-valid-df-mismatch",
            "codigo_imovel": "PROP-VALID-DF",
            "data_assinatura_ccv": "2026-05-10",
            "valor_contrato": "800000.00",
        },
    ]

    vista_gains = [
        # Deal with null DataFinal
        {
            "deal_id": "deal-null-df",
            "property_code": "PROP-NULL-DF",
            "gain_date": None,  # DataFinal=null
            "deal_value": "750000.00",
        },
        # Deal with valid DataFinal 30 days after CCV (delay_days = 30 > 7)
        {
            "deal_id": "deal-valid-df",
            "property_code": "PROP-VALID-DF",
            "gain_date": "2026-06-09",  # DataFinal is valid
            "deal_value": "800000.00",
        },
    ]

    res = reconcile_sales(pipe_txs, vista_gains, date_tolerance_days=7)
    s = res["summary"]
    items = {item["pipeimob_transaction_id"]: item for item in res["items"]}

    # Deal 1: null DataFinal
    item1 = items["tx-null-df"]
    assert item1["vista_gain_date"] is None
    assert item1["delay_days"] is None
    assert "DIVERGENCIA_DATA" not in item1["issues"]
    assert "VINCULADO_COM_DATA_GANHO_NAO_RESOLVIDA" in item1["issues"]
    assert item1["status"] == "VINCULADO_COM_DATA_GANHO_NAO_RESOLVIDA"

    # Deal 2: valid DataFinal outside tolerance
    item2 = items["tx-valid-df-mismatch"]
    assert item2["vista_gain_date"] == "2026-06-09"
    assert item2["delay_days"] == 30
    assert "DIVERGENCIA_DATA" in item2["issues"]
    assert item2["status"] == "DIVERGENCIA_DATA"

    assert s["official_sales"] == 2
    assert s["strictly_matched"] == 0
    assert s["value_only_mismatches"] == 0
    assert s["date_only_mismatches"] == 1
    assert s["linked_with_unresolved_gain_date"] == 1
    assert s["divergent_linked_unique"] == 2
    assert s["total_linked_unique"] == 2
    assert s["non_auditable_gain_dates"] == 1




