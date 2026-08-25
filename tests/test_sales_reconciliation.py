import os
import sys
import pytest
import json
import jwt
import time
import urllib.error
from unittest.mock import patch, MagicMock, AsyncMock

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ["APP_ENV"] = "development"
os.environ["SUPABASE_ISSUER"] = "https://mock.supabase.co/auth/v1"
os.environ["SUPABASE_JWT_AUDIENCE"] = "authenticated"

from fastapi.testclient import TestClient
from main import app, fetch_all_pipeimob_transactions
from services.sales_reconciliation_service import (
    normalize_pipeimob_v2_transaction,
    filter_transactions_by_contract_date,
    reconcile_sales_contract,
    extract_property_address,
    parse_date_to_iso,
    format_currency_decimal,
)
from services.vista_client import (
    VistaSalesClient,
    VistaConfigurationError,
    VistaAuthenticationError,
    VistaTimeoutError,
    VistaResponseError,
    VistaIncompleteQueryError
)


def create_test_jwt(
    email="director@gralhaimoveis.com.br",
    sub="director_test_sub",
    role="authenticated",
    iss="https://mock.supabase.co/auth/v1",
    aud="authenticated",
):
    payload = {
        "email": email,
        "sub": sub,
        "aud": aud,
        "role": role,
        "iss": iss,
        "exp": time.time() + 3600
    }
    return jwt.encode(payload, "secret", algorithm="HS256")


# ==============================================================================
# 1. Mapeamento, Isolamento de Campos e Prioridade de Endereço Pipeimob V2
# ==============================================================================

def test_v2_exact_field_mapping():
    """Valida mapeamento estrito dos campos Pipeimob V2: data_contrato, data_inicio_venda, valor_contrato, total_comissao, agente_gestor."""
    raw_tx = {
        "transacao_unique_id_pipeimob": "tx_1001",
        "codigo_contrato": "CTR-8899",
        "codigo_imovel": "AP-101",
        "data_contrato": "2026-08-10",
        "data_inicio_venda": "2026-08-12",
        "valor_contrato": 1500000.0,
        "total_comissao": 75000.0,
        "agente_gestor": "Carlos Gerente",
        "endereco": "Av. Beira Mar, 500 - Agronômica - Florianópolis"
    }
    
    norm = normalize_pipeimob_v2_transaction(raw_tx)
    
    assert norm["pipeimob_transaction_id"] == "tx_1001"
    assert norm["pipeimob_contract_code"] == "CTR-8899"
    assert norm["property_code"] == "AP-101"
    assert norm["official_sale_date"] == "2026-08-10"
    assert norm["ccv_signature_date"] == "2026-08-10"
    assert norm["ccv_upload_date"] == "2026-08-12"
    assert norm["official_value"] == "1500000.00"
    assert norm["commission_value"] == "75000.00"
    assert norm["pipeimob_manager"] == "Carlos Gerente"
    assert norm["property_address"] == "Av. Beira Mar, 500 - Agronômica - Florianópolis"
    assert norm["missing_fields"] == []
    
    # Aliases
    assert norm["ccv_assinatura"] == "2026-08-10"
    assert norm["subida_ccv"] == "2026-08-12"
    assert norm["vgv"] == "1500000.00"
    assert norm["vgc"] == "75000.00"
    assert norm["endereco"] == "Av. Beira Mar, 500 - Agronômica - Florianópolis"
    assert norm["gerente_pipeimob"] == "Carlos Gerente"


def test_address_priority_order():
    """Valida a prioridade de extração do endereço: 1º endereco, 2º imovel_endereco, 3º composição."""
    # Caso 1: Prioridade 1 (endereco)
    tx1 = {
        "endereco": "Rua Canônica, 100",
        "imovel_endereco": "Rua Secundária, 200",
        "imovel_logradouro": "Rua Terciária",
        "imovel_numero": "300"
    }
    assert extract_property_address(tx1) == "Rua Canônica, 100"
    
    # Caso 2: Fallback para imovel_endereco quando endereco está ausente ou nulo
    tx2 = {
        "endereco": None,
        "imovel_endereco": "Rua Secundária, 200",
        "imovel_logradouro": "Rua Terciária",
        "imovel_numero": "300"
    }
    assert extract_property_address(tx2) == "Rua Secundária, 200"
    
    # Caso 3: Fallback para composição quando ambos endereco e imovel_endereco estão ausentes
    tx3 = {
        "endereco": "",
        "imovel_endereco": None,
        "imovel_logradouro": "Rua das Flores",
        "imovel_numero": "450",
        "imovel_complemento": "Apto 302",
        "imovel_bairro": "Centro",
        "imovel_cidade": "Florianópolis",
        "imovel_uf": "SC",
        "imovel_cep": "88015-100"
    }
    expected_composed = "Rua das Flores, 450 - Apto 302 - Centro - Florianópolis/SC - CEP: 88015-100"
    assert extract_property_address(tx3) == expected_composed
    
    # Caso 4: Todos ausentes
    tx4 = {"endereco": None, "imovel_endereco": None}
    assert extract_property_address(tx4) is None


def test_no_fallback_between_ccv_signature_and_upload():
    """Valida que subida do CCV usa exclusivamente data_inicio_venda e assinatura usa data_contrato."""
    raw_tx = {
        "transacao_unique_id_pipeimob": "tx_1002",
        "data_contrato": "2026-08-05",
        "data_inicio_venda": None,
        "data_assinatura_ccv": "2026-08-01",
        "data_ccv": "2026-08-02",
        "valor_contrato": 500000.0,
        "total_comissao": 25000.0
    }
    
    norm = normalize_pipeimob_v2_transaction(raw_tx)
    
    assert norm["ccv_signature_date"] == "2026-08-05"
    assert norm["official_sale_date"] == "2026-08-05"
    assert norm["ccv_upload_date"] is None
    assert norm["subida_ccv"] is None


def test_missing_data_contrato_generates_incomplete_data():
    """Valida que ausência de data_contrato gera DADO_FONTE_INCOMPLETO sem fallback silencioso para data_assinatura_ccv."""
    raw_tx = {
        "transacao_unique_id_pipeimob": "tx_1003",
        "data_contrato": None,
        "data_assinatura_ccv": "2026-08-15",
        "data_inicio_venda": "2026-08-16",
        "valor_contrato": 600000.0,
        "total_comissao": 30000.0
    }
    
    in_period, incomplete = filter_transactions_by_contract_date([raw_tx], "2026-08-01", "2026-08-31")
    assert len(in_period) == 0
    assert len(incomplete) == 1
    assert "data_contrato" in incomplete[0]["missing_fields"]
    
    contract = reconcile_sales_contract([raw_tx], [], "2026-08-01", "2026-08-31")
    assert contract["summary"]["official_sales"] == 0
    assert contract["summary"]["source_data_incomplete"] == 1
    assert contract["items"][0]["status"] == "DADO_FONTE_INCOMPLETO"


def test_strict_zero_preservation_and_null_distinction():
    """Valida preservação estrita de zero em VGC e VGV, sem coerção por truthiness (zero é válido, nulo é ausente)."""
    tx_zero = {
        "transacao_unique_id_pipeimob": "tx_zero",
        "data_contrato": "2026-08-10",
        "valor_contrato": 0.0,
        "total_comissao": 0.0,
        "comissao_imobiliaria": 50000.0
    }
    
    norm_zero = normalize_pipeimob_v2_transaction(tx_zero)
    assert norm_zero["official_value"] == "0.00"
    assert norm_zero["commission_value"] == "0.00"
    
    tx_null = {
        "transacao_unique_id_pipeimob": "tx_null",
        "data_contrato": "2026-08-10",
        "valor_contrato": None,
        "total_comissao": None
    }
    norm_null = normalize_pipeimob_v2_transaction(tx_null)
    assert norm_null["official_value"] is None
    assert norm_null["commission_value"] is None


# ==============================================================================
# 2. Regras de Conciliação, CRM Vista e Tratamento de Ganhos Sem Data
# ==============================================================================

def test_vista_won_deal_missing_gain_date_handled_in_data_quality():
    """
    Valida que negócio Ganho no Vista sem data de ganho/fechamento:
    - NÃO é incluído nas vendas, conciliações ou pendências do período;
    - NÃO desaparece silenciosamente;
    - É contabilizado em summary.vista_won_missing_gain_date;
    - É reportado com identificação técnica mínima no bloco data_quality.
    """
    pipe_tx = {
        "transacao_unique_id_pipeimob": "tx_normal",
        "codigo_contrato": "CTR-NORM",
        "codigo_imovel": "AP-NORMAL",
        "data_contrato": "2026-08-15",
        "valor_contrato": 1000000.0,
        "total_comissao": 50000.0,
        "agente_gestor": "Gestor 1"
    }
    
    vista_deals = [
        # Negócio normal com data
        {
            "deal_id": "deal_ok",
            "codigo_imovel": "AP-NORMAL",
            "status": "Ganho",
            "valor": 1000000.0,
            "data_fechamento": "2026-08-15",
            "corretor_nome": "Corretor OK"
        },
        # Negócio Ganho SEM data de fechamento/ganho
        {
            "deal_id": "deal_no_date_1",
            "codigo_imovel": "AP-SEM-DATA",
            "status": "Ganho",
            "valor": 850000.0,
            "data_fechamento": None,
            "data_ganho": None,
            "corretor_nome": "Corretor Sem Data"
        }
    ]
    
    res = reconcile_sales_contract([pipe_tx], vista_deals, "2026-08-01", "2026-08-31")
    
    # 1. KPIs do período devem refletir apenas a venda válida
    assert res["summary"]["official_sales"] == 1
    assert res["summary"]["matched"] == 1
    assert res["summary"]["vista_without_pipeimob_contract"] == 0
    assert res["summary"]["pipeimob_without_vista_gain"] == 0
    
    # 2. Contador de qualidade deve registrar 1 item sem data
    assert res["summary"]["vista_won_missing_gain_date"] == 1
    
    # 3. Bloco data_quality deve conter o detalhamento técnico do registro excluído
    dq = res["data_quality"]
    assert dq["vista_won_missing_gain_date_count"] == 1
    assert len(dq["vista_won_missing_gain_date_items"]) == 1
    
    dq_item = dq["vista_won_missing_gain_date_items"][0]
    assert dq_item["vista_deal_id"] == "deal_no_date_1"
    assert dq_item["property_code"] == "AP-SEM-DATA"
    assert dq_item["commercial_broker"] == "Corretor Sem Data"
    assert dq_item["vista_value"] == "850000.00"
    assert "sem data de ganho/fechamento" in dq_item["reason"]
    assert "documentos" not in dq_item and "cpf" not in json.dumps(dq_item).lower()


def test_fechamento_stage_is_not_sale_only_ganho_is_sale():
    """
    Valida regra de negócio:
    'Fechamento' é etapa do funil. Apenas status 'Ganho' representa venda no CRM Vista.
    """
    pipe_tx = {
        "transacao_unique_id_pipeimob": "tx_2001",
        "codigo_contrato": "CTR-9901",
        "codigo_imovel": "IMOV-77",
        "data_contrato": "2026-08-14",
        "valor_contrato": 1000000.0,
        "total_comissao": 50000.0,
        "agente_gestor": "Gestor X"
    }
    
    vista_lost_in_fechamento = [{
        "deal_id": "deal_1",
        "codigo_imovel": "IMOV-77",
        "status": "Perdido",
        "etapa": "Fechamento",
        "valor": 1000000.0,
        "corretor_nome": "Corretor A"
    }]
    
    res1 = reconcile_sales_contract([pipe_tx], vista_lost_in_fechamento, "2026-08-01", "2026-08-31")
    assert res1["summary"]["matched"] == 0
    assert res1["summary"]["pipeimob_without_vista_gain"] == 1
    assert res1["items"][0]["status"] == "PIPEIMOB_SEM_GANHO_VISTA"
    
    vista_won = [{
        "deal_id": "deal_2",
        "codigo_imovel": "IMOV-77",
        "status": "Ganho",
        "etapa": "Fechamento",
        "valor": 1000000.0,
        "data_fechamento": "2026-08-14",
        "corretor_nome": "Corretor B"
    }]
    
    res2 = reconcile_sales_contract([pipe_tx], vista_won, "2026-08-01", "2026-08-31")
    assert res2["summary"]["matched"] == 1
    assert res2["summary"]["pipeimob_without_vista_gain"] == 0
    assert res2["items"][0]["status"] == "CONCILIADO"
    assert res2["items"][0]["commercial_broker"] == "Corretor B"
    assert res2["items"][0]["pipeimob_manager"] == "Gestor X"


def test_vista_only_won_deal_emitted_as_vista_sem_contrato_pipeimob():
    """Valida que negócio Ganho existente apenas no Vista é incluído no resultado como VISTA_SEM_CONTRATO_PIPEIMOB."""
    vista_only_deal = [{
        "deal_id": "deal_vista_only",
        "codigo_imovel": "IMOV-VISTA-100",
        "status": "Ganho",
        "etapa": "Ganho",
        "valor": 750000.0,
        "data_fechamento": "2026-08-18",
        "corretor_nome": "Corretor Exclusivo Vista"
    }]
    
    res = reconcile_sales_contract([], vista_only_deal, "2026-08-01", "2026-08-31")
    
    assert res["summary"]["official_sales"] == 0
    assert res["summary"]["matched"] == 0
    assert res["summary"]["vista_without_pipeimob_contract"] == 1
    
    assert len(res["items"]) == 1
    item = res["items"][0]
    assert item["status"] == "VISTA_SEM_CONTRATO_PIPEIMOB"
    assert item["vista_deal_id"] == "deal_vista_only"
    assert item["property_code"] == "IMOV-VISTA-100"
    assert item["commercial_broker"] == "Corretor Exclusivo Vista"
    assert item["vista_value"] == "750000.00"
    assert item["pipeimob_contract_code"] is None


def test_multiple_won_deals_resolved_by_secondary_criteria():
    """Valida que quando há múltiplos ganhos para o mesmo imóvel, usa valor e data para desempate secundário determinístico."""
    pipe_tx = {
        "transacao_unique_id_pipeimob": "tx_multi",
        "codigo_contrato": "CTR-MULTI",
        "codigo_imovel": "IMOV-99",
        "data_contrato": "2026-08-10",
        "valor_contrato": 1200000.0,
        "total_comissao": 60000.0,
        "agente_gestor": "Gestor M"
    }
    
    vista_deals = [
        {
            "deal_id": "deal_wrong",
            "codigo_imovel": "IMOV-99",
            "status": "Ganho",
            "valor": 500000.0,
            "data_fechamento": "2026-08-01",
            "corretor_nome": "Corretor Antigo"
        },
        {
            "deal_id": "deal_correct",
            "codigo_imovel": "IMOV-99",
            "status": "Ganho",
            "valor": 1200000.0,
            "data_fechamento": "2026-08-10",
            "corretor_nome": "Corretor Oficial"
        }
    ]
    
    res = reconcile_sales_contract([pipe_tx], vista_deals, "2026-08-01", "2026-08-31")
    assert res["summary"]["matched"] == 1
    assert res["summary"]["ambiguous_vista_deals"] == 0
    assert res["items"][0]["status"] == "CONCILIADO"
    assert res["items"][0]["vista_deal_id"] == "deal_correct"
    assert res["items"][0]["commercial_broker"] == "Corretor Oficial"


def test_multiple_won_deals_unresolvable_generates_ambiguity():
    """Valida que múltiplos negócios 'Ganho' não desempatáveis geram AMBIGUIDADE_MULTIPLOS_GANHOS_VISTA sem escolha arbitrária."""
    pipe_tx = {
        "transacao_unique_id_pipeimob": "tx_ambig",
        "codigo_contrato": "CTR-AMBIG",
        "codigo_imovel": "IMOV-DUP",
        "data_contrato": "2026-08-20",
        "valor_contrato": 2000000.0,
        "total_comissao": 100000.0,
        "agente_gestor": "Gestor Ambig"
    }
    
    multiple_identical_vista_deals = [
        {
            "deal_id": "deal_A",
            "codigo_imovel": "IMOV-DUP",
            "status": "Ganho",
            "valor": 2000000.0,
            "data_fechamento": "2026-08-20",
            "corretor_nome": "Corretor 1"
        },
        {
            "deal_id": "deal_B",
            "codigo_imovel": "IMOV-DUP",
            "status": "Ganho",
            "valor": 2000000.0,
            "data_fechamento": "2026-08-20",
            "corretor_nome": "Corretor 2"
        }
    ]
    
    res = reconcile_sales_contract([pipe_tx], multiple_identical_vista_deals, "2026-08-01", "2026-08-31")
    assert res["summary"]["matched"] == 0
    assert res["summary"]["ambiguous_vista_deals"] == 1
    
    item = res["items"][0]
    assert item["status"] == "AMBIGUIDADE_MULTIPLOS_GANHOS_VISTA"
    assert item["candidates_count"] == 2
    assert item["candidate_deal_ids"] == ["deal_A", "deal_B"]
    assert item["commercial_broker"] is None


def test_manager_presented_as_manager_never_as_broker():
    """Valida que agente_gestor é apresentado exclusivamente como gerente e nunca confunde com o corretor comercial."""
    pipe_tx = {
        "transacao_unique_id_pipeimob": "tx_mgr",
        "codigo_imovel": "IMOV-10",
        "data_contrato": "2026-08-10",
        "valor_contrato": 800000.0,
        "total_comissao": 40000.0,
        "agente_gestor": "Luciano Gerente"
    }
    vista_deal = [{
        "deal_id": "deal_10",
        "codigo_imovel": "IMOV-10",
        "status": "Ganho",
        "valor": 800000.0,
        "data_fechamento": "2026-08-10",
        "corretor_nome": "Marcos Corretor"
    }]
    
    res = reconcile_sales_contract([pipe_tx], vista_deal, "2026-08-01", "2026-08-31")
    item = res["items"][0]
    
    assert item["pipeimob_manager"] == "Luciano Gerente"
    assert item["commercial_broker"] == "Marcos Corretor"
    assert item["manager_and_broker_differ"] is True


# ==============================================================================
# 3. Semântica Temporal Dinâmica e Períodos Vazios
# ==============================================================================

def test_inclusive_period_filtering():
    """Valida limites inclusivos do filtro temporal sobre data_contrato."""
    txs = [
        {"transacao_unique_id_pipeimob": "t1", "data_contrato": "2026-08-01", "valor_contrato": 100.0, "total_comissao": 5.0},
        {"transacao_unique_id_pipeimob": "t2", "data_contrato": "2026-08-15", "valor_contrato": 200.0, "total_comissao": 10.0},
        {"transacao_unique_id_pipeimob": "t3", "data_contrato": "2026-08-31", "valor_contrato": 300.0, "total_comissao": 15.0},
        {"transacao_unique_id_pipeimob": "t4", "data_contrato": "2026-07-31", "valor_contrato": 400.0, "total_comissao": 20.0},
        {"transacao_unique_id_pipeimob": "t5", "data_contrato": "2026-09-01", "valor_contrato": 500.0, "total_comissao": 25.0},
    ]
    
    in_period, _ = filter_transactions_by_contract_date(txs, "2026-08-01", "2026-08-31")
    ids = [t["pipeimob_transaction_id"] for t in in_period]
    
    assert ids == ["t1", "t2", "t3"]


def test_distinct_periods_return_distinct_sets():
    """Valida que consultas de períodos diferentes retornam conjuntos independentes."""
    tx_aug = {"transacao_unique_id_pipeimob": "aug_1", "data_contrato": "2026-08-10", "valor_contrato": 1000.0, "total_comissao": 50.0}
    tx_sep = {"transacao_unique_id_pipeimob": "sep_1", "data_contrato": "2026-09-10", "valor_contrato": 2000.0, "total_comissao": 100.0}
    
    all_txs = [tx_aug, tx_sep]
    
    aug_res, _ = filter_transactions_by_contract_date(all_txs, "2026-08-01", "2026-08-31")
    sep_res, _ = filter_transactions_by_contract_date(all_txs, "2026-09-01", "2026-09-30")
    
    assert [t["pipeimob_transaction_id"] for t in aug_res] == ["aug_1"]
    assert [t["pipeimob_transaction_id"] for t in sep_res] == ["sep_1"]


def test_empty_period_returns_clean_zeroes():
    """Valida que período sem vendas retorna resumo zerado e lista vazia sem erros."""
    contract = reconcile_sales_contract([], [], "2026-10-01", "2026-10-31")
    
    assert contract["summary"]["official_sales"] == 0
    assert contract["summary"]["official_vgv"] == "0.00"
    assert contract["summary"]["official_vgc"] == "0.00"
    assert contract["summary"]["matched"] == 0
    assert contract["summary"]["pipeimob_without_vista_gain"] == 0
    assert contract["summary"]["vista_without_pipeimob_contract"] == 0
    assert contract["items"] == []


# ==============================================================================
# 4. Testes de Paginação e Integração de Clientes
# ==============================================================================

def test_pipeimob_pagination_fetches_multiple_pages():
    """Valida paginação do Pipeimob em fetch_all_pipeimob_transactions."""
    mock_responses = {
        1: {
            "success": True,
            "meta": {
                "pagination": {"total": 3, "total_pages": 2, "current_page": 1, "per_page": 2}
            },
            "data": {
                "transacoes": [
                    {"transacao_unique_id": "tx_p1_1", "data_contrato": "2026-08-05"},
                    {"transacao_unique_id": "tx_p1_2", "data_contrato": "2026-08-06"}
                ]
            }
        },
        2: {
            "success": True,
            "meta": {
                "pagination": {"total": 3, "total_pages": 2, "current_page": 2, "per_page": 2}
            },
            "data": {
                "transacoes": [
                    {"transacao_unique_id": "tx_p2_1", "data_contrato": "2026-08-07"}
                ]
            }
        }
    }
    
    with patch("main.get_auth_token", return_value="mock_pipe_token"), \
         patch("urllib.request.urlopen") as mock_open:
        
        mock_resp_1 = MagicMock()
        mock_resp_1.read.return_value = json.dumps(mock_responses[1]).encode("utf-8")
        mock_resp_2 = MagicMock()
        mock_resp_2.read.return_value = json.dumps(mock_responses[2]).encode("utf-8")
        mock_resp_3 = MagicMock()
        mock_resp_3.read.return_value = json.dumps({"success": True, "meta": {"pagination": {"total": 3, "total_pages": 2, "current_page": 3}}, "data": {"transacoes": []}}).encode("utf-8")
        
        mock_open.return_value.__enter__.side_effect = [mock_resp_1, mock_resp_2, mock_resp_3]
        
        txs, pages = fetch_all_pipeimob_transactions(
            api_key="k",
            api_secret="s",
            data_inicio_ccv="2026-08-01",
            data_fim_ccv="2026-08-31"
        )
        
        assert len(txs) == 3
        assert pages >= 2


def test_vista_client_sends_exact_status_datafinal_and_pipe():
    """Valida envio exato de Status='Ganho', DataFinal=[start, end] normalizado e codigo_pipe para o Vista."""
    client_mock = VistaSalesClient(
        api_key="valid_key",
        sales_pipe_id="pipe_vendas_123"
    )
    
    captured_calls = []
    
    def mock_api_get(endpoint, params):
        captured_calls.append({"endpoint": endpoint, "params": params})
        return {"total": "0"}
        
    with patch.object(client_mock, "_api_get", side_effect=mock_api_get):
        client_mock.fetch_won_deals("2026-08-01", "2026-08-31")
        
    assert len(captured_calls) == 1
    call = captured_calls[0]
    
    assert call["params"]["codigo_pipe"] == "pipe_vendas_123"
    pesquisa = json.loads(call["params"]["pesquisa"])
    assert pesquisa["filter"]["Status"] == "Ganho"
    assert pesquisa["filter"]["DataFinal"] == ["2026-08-01 00:00:00", "2026-08-31 23:59:59"]
    assert pesquisa["paginacao"]["pagina"] == 1
    assert pesquisa["paginacao"]["quantidade"] == 50


def test_vista_client_pagination_with_more_than_50_deals():
    """Valida paginação correta do Vista com mais de 50 negócios baseada em registros brutos."""
    client_mock = VistaSalesClient(api_key="valid_key")
    
    page_1_data = {"total": "75", "paginas": "2", "pagina": "1", "quantidade": "50"}
    for i in range(1, 51):
        page_1_data[str(i)] = {"Codigo": f"deal_{i}", "CodigoImovel": f"AP-{i}", "Status": "Ganho", "ValorNegocio": 100000.0 * i}
        
    page_2_data = {"total": "75", "paginas": "2", "pagina": "2", "quantidade": "25"}
    for i in range(51, 76):
        page_2_data[str(i)] = {"Codigo": f"deal_{i}", "CodigoImovel": f"AP-{i}", "Status": "Ganho", "ValorNegocio": 100000.0 * i}
        
    def mock_api_get(endpoint, params):
        pesq = json.loads(params["pesquisa"])
        pag = pesq["paginacao"]
        if pag["pagina"] == 1:
            return page_1_data
        elif pag["pagina"] == 2:
            return page_2_data
        return {"total": "75"}
        
    with patch.object(client_mock, "_api_get", side_effect=mock_api_get):
        deals = client_mock.fetch_won_deals("2026-08-01", "2026-08-31")
        assert len(deals) == 75
        assert deals[0]["Codigo"] == "deal_1"
        assert deals[74]["Codigo"] == "deal_75"


def test_vista_client_user_pagination():
    """Valida paginação de usuários no Vista."""
    client_mock = VistaSalesClient(api_key="valid_key")
    
    page_1 = {"total": "60", "paginas": "2"}
    for i in range(1, 51):
        page_1[str(i)] = {"Codigo": f"u_{i}", "Nomecompleto": f"Corretor {i}"}
        
    page_2 = {"total": "60", "paginas": "2"}
    for i in range(51, 61):
        page_2[str(i)] = {"Codigo": f"u_{i}", "Nomecompleto": f"Corretor {i}"}
        
    def mock_api_get(endpoint, params):
        pesq = json.loads(params["pesquisa"])
        pag = pesq["paginacao"]
        if pag["pagina"] == 1:
            return page_1
        elif pag["pagina"] == 2:
            return page_2
        return {}
        
    with patch.object(client_mock, "_api_get", side_effect=mock_api_get):
        users_map = client_mock.fetch_users_map()
        assert len(users_map) == 60
        assert users_map["u_1"] == "Corretor 1"
        assert users_map["u_60"] == "Corretor 60"


# ==============================================================================
# 5. Tratamento de Exceções Tipadas do Vista e Falhas Upstream
# ==============================================================================

def test_vista_client_handles_401_error():
    """Valida que erro 401 levanta VistaAuthenticationError."""
    client_mock = VistaSalesClient(api_key="invalid_key")
    
    with patch("urllib.request.urlopen") as mock_url:
        http_err = urllib.error.HTTPError(
            url="https://gralhaim-rest.vistahost.com.br/negocios/listar",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None
        )
        mock_url.side_effect = http_err
        
        with pytest.raises(VistaAuthenticationError):
            client_mock.fetch_won_deals("2026-08-01", "2026-08-31")


def test_vista_client_handles_timeout():
    """Valida que timeout levanta VistaTimeoutError."""
    client_mock = VistaSalesClient(api_key="valid_key")
    
    with patch("urllib.request.urlopen") as mock_url:
        url_err = urllib.error.URLError(reason="timed out")
        mock_url.side_effect = url_err
        
        with pytest.raises(VistaTimeoutError):
            client_mock.fetch_won_deals("2026-08-01", "2026-08-31")


def test_vista_client_handles_invalid_json():
    """Valida que resposta inválida/HTML levanta VistaResponseError."""
    client_mock = VistaSalesClient(api_key="valid_key")
    
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"<html>502 Bad Gateway</html>"
    
    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(VistaResponseError):
            client_mock.fetch_won_deals("2026-08-01", "2026-08-31")


# ==============================================================================
# 6. Teste de Integração Completo do Endpoint /api/reconciliation/sales
# ==============================================================================

def test_reconciliation_endpoint_integrates_both_sources():
    """
    Valida de ponta a ponta que o endpoint HTTP /api/reconciliation/sales:
    1. Chama o dataset do Pipeimob V2;
    2. Consulta o cliente CRM Vista de forma assíncrona;
    3. Retorna o contrato versionado com conciliação determinística e corretor comercial;
    4. Não expõe nenhum dado pessoal de clientes (PII);
    5. Retorna o bloco data_quality com status dos registros.
    """
    mock_pipe_dataset = [
        {
            "transacao_unique_id_pipeimob": "tx_int_1",
            "codigo_contrato": "CTR-INT-100",
            "codigo_imovel": "IMOV-999",
            "data_contrato": "2026-08-15",
            "data_inicio_venda": "2026-08-16",
            "valor_contrato": 2500000.0,
            "total_comissao": 125000.0,
            "agente_gestor": "Rodrigo Gestor",
            "endereco": "Rua Central, 1000 - Centro - Florianópolis"
        }
    ]
    
    mock_vista_deals = [
        {
            "deal_id": "deal_int_1",
            "codigo_imovel": "IMOV-999",
            "status": "Ganho",
            "etapa": "Fechamento",
            "valor": 2500000.0,
            "data_fechamento": "2026-08-15",
            "corretor_nome": "Juliana Corretora",
            "corretor_codigo": "corr_12"
        }
    ]
    
    token = create_test_jwt()
    test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    
    with patch("main.load_transactions_dataset", new_callable=AsyncMock) as mock_load, \
         patch("services.vista_client.VistaSalesClient.get_enriched_won_deals") as mock_vista_get:
        
        mock_load.return_value = ("live", "pipeimob_api", mock_pipe_dataset, 1, "miss")
        mock_vista_get.return_value = mock_vista_deals
        
        response = test_client.get(
            "/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31"
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert mock_load.called
        assert mock_vista_get.called
        
        assert data["contract_version"] == "director-sales-reconciliation-v2.0"
        assert data["summary"]["official_sales"] == 1
        assert data["summary"]["matched"] == 1
        assert data["summary"]["official_vgv"] == "2500000.00"
        assert data["summary"]["official_vgc"] == "125000.00"
        assert "vista_won_missing_gain_date" in data["summary"]
        assert "data_quality" in data
        
        item = data["items"][0]
        assert item["status"] == "CONCILIADO"
        assert item["pipeimob_contract_code"] == "CTR-INT-100"
        assert item["property_code"] == "IMOV-999"
        assert item["official_sale_date"] == "2026-08-15"
        assert item["ccv_signature_date"] == "2026-08-15"
        assert item["ccv_upload_date"] == "2026-08-16"
        assert item["commercial_broker"] == "Juliana Corretora"
        assert item["pipeimob_manager"] == "Rodrigo Gestor"
        assert item["property_address"] == "Rua Central, 1000 - Centro - Florianópolis"
        
        serialized = json.dumps(data)
        assert "cpf" not in serialized.lower()
        assert "email_cliente" not in serialized.lower()
        assert "telefone" not in serialized.lower()
        assert "nome_cliente" not in serialized.lower()


def test_reconciliation_endpoint_propagates_upstream_vista_failure():
    """Valida que falha upstream no Vista (ex: 504 Timeout) retorna erro HTTP sanitizado em vez de mascarar como zero vendas."""
    token = create_test_jwt()
    test_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    
    with patch("main.load_transactions_dataset", new_callable=AsyncMock) as mock_load, \
         patch("services.vista_client.VistaSalesClient.get_enriched_won_deals") as mock_vista_get:
        
        mock_load.return_value = ("live", "pipeimob_api", [{"data_contrato": "2026-08-10"}], 1, "miss")
        mock_vista_get.side_effect = VistaTimeoutError("Tempo limite excedido")
        
        response = test_client.get(
            "/api/reconciliation/sales?data_inicio_ccv=2026-08-01&data_fim_ccv=2026-08-31"
        )
        
        assert response.status_code == 504
        assert "Tempo limite" in response.json()["detail"]


def test_vista_client_contract_compliance_and_stage_isolation():
    """
    Comprova os requisitos de conformidade com o CRM Vista:
    1. Apenas campos documentados em NEGOCIO_FIELDS são enviados na requisição;
    2. Negócios em 'Fechamento' são rigorosamente ignorados e NÃO são classificados como venda;
    3. Negócios com Status='Ganho' são normalizados e classificados como venda;
    4. Nenhuma chave de API ou credencial vaza em exceções ou logs.
    """
    secret_key = "super_secret_vista_key_xyz_123"
    client_mock = VistaSalesClient(api_key=secret_key, sales_pipe_id="1")

    # Mock de payload retornado pelo Vista
    mock_payload = {
        "total": "3",
        "paginas": "1",
        "1": {
            "Codigo": "deal_won_1",
            "Status": "Ganho",
            "NomeEtapa": "Contrato Assinado",
            "ValorNegocio": 1500000.00,
            "DataFinal": "2026-08-15 14:30:00",
            "CodigoImovel": "IMOVEL-777",
            "CodigoUsuario": "usr_99",
            "NomeUsuario": "Corretor Comercial A"
        },
        "2": {
            "Codigo": "deal_fechamento_2",
            "Status": "Fechamento",  # Etapa do funil - NÃO É VENDA
            "NomeEtapa": "Fechamento",
            "ValorNegocio": 800000.00,
            "DataFinal": "2026-08-20 10:00:00",
            "CodigoImovel": "IMOVEL-888",
            "CodigoUsuario": "usr_99"
        },
        "3": {
            "Codigo": "deal_perdido_3",
            "Status": "Perdido",
            "NomeEtapa": "Proposta Recusada",
            "ValorNegocio": 500000.00,
            "DataFinal": "2026-08-22 11:00:00",
            "CodigoImovel": "IMOVEL-999"
        }
    }

    captured_params = []
    def mock_get(endpoint, params):
        captured_params.append(params)
        return mock_payload

    with patch.object(client_mock, "_api_get", side_effect=mock_get), \
         patch.object(client_mock, "fetch_users_map", return_value={"usr_99": "Corretor Comercial A"}):
        
        deals = client_mock.get_enriched_won_deals("2026-08-01", "2026-08-25")

    # 1. Validação da requisição enviada
    assert len(captured_params) == 1
    pesquisa = json.loads(captured_params[0]["pesquisa"])
    documented_fields = {
        "Codigo", "NomePipe", "UltimaAtualizacao", "NomeNegocio", "Status",
        "DataInicial", "DataFinal", "ValorNegocio", "PrevisaoFechamento",
        "VeiculoCaptacao", "CodigoMotivoPerda", "MotivoPerda", "ObservacaoPerda",
        "CodigoPipe", "EtapaAtual", "NomeEtapa", "CodigoCliente", "NomeCliente",
        "CodigoImovel", "StatusAtividades", "CodigoUsuario", "NomeUsuario"
    }
    for field in pesquisa["fields"]:
        assert field in documented_fields

    assert pesquisa["filter"]["Status"] == "Ganho"
    assert pesquisa["filter"]["DataFinal"] == ["2026-08-01 00:00:00", "2026-08-25 23:59:59"]
    assert captured_params[0]["codigo_pipe"] == "1"

    # 2. Validação do isolamento de etapas: apenas 1 negócio Ganho retornado
    assert len(deals) == 1
    deal = deals[0]
    assert deal["deal_id"] == "deal_won_1"
    assert deal["status"] == "Ganho"
    assert deal["valor"] == 1500000.00
    assert deal["data_fechamento"] == "2026-08-15 14:30:00"
    assert deal["codigo_imovel"] == "IMOVEL-777"
    assert deal["corretor_nome"] == "Corretor Comercial A"

    # 3. Validação de não-vazamento de credenciais
    with patch("urllib.request.urlopen", side_effect=RuntimeError("Simulated connection error")):
        try:
            client_mock._api_get("negocios/listar", {"pesquisa": "{}"})
        except Exception as e:
            assert secret_key not in str(e)
