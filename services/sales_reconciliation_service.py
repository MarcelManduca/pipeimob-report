"""
Serviço Canônico de Conciliação de Vendas Pipeimob V2 × CRM Vista.
Contrato versionado: director-sales-reconciliation-v2.0
"""

import re
from collections import defaultdict
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Set


def parse_date_to_iso(val: Any) -> Optional[str]:
    """Converte valores variados de data para string YYYY-MM-DD."""
    if val is None:
        return None
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("null", "none", "n/d", "0000-00-00", "0000-00-00 00:00:00"):
        return None
    # Truncate at space or T
    if " " in val_str:
        val_str = val_str.split(" ")[0]
    elif "T" in val_str:
        val_str = val_str.split("T")[0]
    if len(val_str) == 10 and val_str[4] == "-" and val_str[7] == "-":
        return val_str
    # Try parsing DD/MM/YYYY
    if len(val_str) == 10 and val_str[2] == "/" and val_str[5] == "/":
        parts = val_str.split("/")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return None


def format_currency_decimal(val: Any) -> Optional[str]:
    """
    Formata valor decimal preservando zero explicitamente.
    Retorna None apenas se o campo for nulo/ausente.
    """
    if val is None:
        return None
    val_str = str(val).strip()
    if val_str == "" or val_str.lower() in ("null", "none"):
        return None
    try:
        dec = Decimal(val_str.replace("R$", "").replace(" ", "").replace(",", "."))
        return f"{dec:.2f}"
    except Exception:
        return None


def extract_property_address(tx: Dict[str, Any]) -> Optional[str]:
    """
    Extrai o endereço do imóvel conforme ordem estrita de prioridade:
    1. endereco
    2. imovel_endereco
    3. Composição de logradouro, numero, complemento, bairro, cidade, UF e CEP.
    """
    # Prioridade 1: endereco
    raw_end = tx.get("endereco")
    if raw_end is not None and str(raw_end).strip() and str(raw_end).strip().lower() not in ("null", "none"):
        return str(raw_end).strip()

    # Prioridade 2: imovel_endereco
    raw_imov_end = tx.get("imovel_endereco")
    if raw_imov_end is not None and str(raw_imov_end).strip() and str(raw_imov_end).strip().lower() not in ("null", "none"):
        return str(raw_imov_end).strip()

    # Prioridade 3: Composição de logradouro, número, complemento, bairro, cidade, UF e CEP
    logradouro = tx.get("logradouro") or tx.get("imovel_logradouro") or tx.get("rua") or tx.get("imovel_rua")
    numero = tx.get("numero") or tx.get("imovel_numero")
    complemento = tx.get("complemento") or tx.get("imovel_complemento")
    bairro = tx.get("bairro") or tx.get("imovel_bairro")
    cidade = tx.get("cidade") or tx.get("imovel_cidade")
    uf = tx.get("uf") or tx.get("imovel_uf") or tx.get("estado") or tx.get("imovel_estado")
    cep = tx.get("cep") or tx.get("imovel_cep")

    parts = []
    street_part = ""
    if logradouro and str(logradouro).strip():
        street_part = str(logradouro).strip()
        if numero and str(numero).strip():
            street_part += f", {str(numero).strip()}"
        if complemento and str(complemento).strip():
            street_part += f" - {str(complemento).strip()}"
        parts.append(street_part)
    elif numero and str(numero).strip():
        parts.append(f"Nº {str(numero).strip()}")

    if bairro and str(bairro).strip():
        parts.append(str(bairro).strip())
    if cidade and str(cidade).strip():
        cid_str = str(cidade).strip()
        if uf and str(uf).strip():
            cid_str += f"/{str(uf).strip()}"
        parts.append(cid_str)
    elif uf and str(uf).strip():
        parts.append(str(uf).strip())

    if cep and str(cep).strip():
        parts.append(f"CEP: {str(cep).strip()}")

    if parts:
        return " - ".join(parts)
    return None


def normalize_pipeimob_v2_transaction(tx: Dict[str, Any], mode: str = "live") -> Dict[str, Any]:
    """
    Normaliza uma transação da API Pipeimob V2 conforme regras canônicas:
    - data_contrato = data oficial da venda / CCV assinado;
    - data_inicio_venda = data de subida do CCV;
    - valor_contrato = VGV;
    - total_comissao = VGC (seleção por presença/nulidade, sem truthiness);
    - agente_gestor = gerente responsável;
    - endereco = endereço com prioridades estritas.
    """
    raw_contrato = tx.get("data_contrato")
    raw_subida = tx.get("data_inicio_venda")
    
    official_sale_date = parse_date_to_iso(raw_contrato)
    ccv_signature_date = official_sale_date
    
    # Subida do CCV é estritamente data_inicio_venda, sem fallback
    ccv_upload_date = parse_date_to_iso(raw_subida)
    
    # VGV: valor_contrato estrito
    raw_vgv = tx.get("valor_contrato")
    official_value = format_currency_decimal(raw_vgv)
    
    # VGC: total_comissao estrito (nunca total_comissao or comissao_imobiliaria)
    raw_vgc = tx.get("total_comissao")
    commission_value = format_currency_decimal(raw_vgc)
    
    # Gerente responsável: agente_gestor
    manager = tx.get("agente_gestor")
    if manager is not None:
        manager = str(manager).strip()
        if manager == "" or manager.lower() in ("null", "none"):
            manager = None
            
    # Códigos identificadores
    tx_id = str(tx.get("transacao_unique_id_pipeimob") or tx.get("transacao_unique_id") or tx.get("id") or "").strip() or None
    contract_code = str(tx.get("codigo_contrato") or "").strip() or None
    property_code = str(tx.get("codigo_imovel") or "").strip() or None
    
    address = extract_property_address(tx)
    
    # Missing fields audit
    missing_fields = []
    if official_sale_date is None:
        missing_fields.append("data_contrato")
    if official_value is None:
        missing_fields.append("valor_contrato")
    if commission_value is None:
        missing_fields.append("total_comissao")
    if manager is None:
        missing_fields.append("agente_gestor")
        
    return {
        "pipeimob_transaction_id": tx_id,
        "pipeimob_contract_code": contract_code,
        "property_code": property_code,
        "official_sale_date": official_sale_date,
        "ccv_signature_date": ccv_signature_date,
        "ccv_upload_date": ccv_upload_date,
        "official_value": official_value,
        "commission_value": commission_value,
        "commission_date": parse_date_to_iso(tx.get("data_pagamento_comissao_prevista") or tx.get("data_pagamento_comissao")),
        "property_address": address,
        "pipeimob_manager": manager,
        "raw_stage": tx.get("etapa_atual"),
        "missing_fields": missing_fields,
        # Aliases para compatibilidade de contrato
        "ccv_assinatura": ccv_signature_date,
        "subida_ccv": ccv_upload_date,
        "vgv": official_value,
        "vgc": commission_value,
        "endereco": address,
        "gerente_pipeimob": manager
    }


def filter_transactions_by_contract_date(
    transactions: List[Dict[str, Any]],
    start_date: str,
    end_date: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Filtra transações inclusivamente por data_contrato.
    Retorna (dentro_periodo, sem_data_ou_fora).
    """
    start_iso = parse_date_to_iso(start_date)
    end_iso = parse_date_to_iso(end_date)
    
    in_period = []
    incomplete = []
    
    for tx in transactions:
        norm = normalize_pipeimob_v2_transaction(tx)
        sale_date = norm["official_sale_date"]
        
        if not sale_date:
            incomplete.append(norm)
            continue
            
        if start_iso and sale_date < start_iso:
            continue
        if end_iso and sale_date > end_iso:
            continue
            
        in_period.append(norm)
        
    return in_period, incomplete


def reconcile_sales_contract(
    pipeimob_transactions: List[Dict[str, Any]],
    vista_deals: List[Dict[str, Any]],
    start_date: str,
    end_date: str,
    mode: str = "live"
) -> Dict[str, Any]:
    """
    Executa a conciliação canônica entre Pipeimob V2 e CRM Vista.
    Gera o contrato versionado 'director-sales-reconciliation-v2.0'.
    """
    start_iso = parse_date_to_iso(start_date) or start_date
    end_iso = parse_date_to_iso(end_date) or end_date
    
    # 1. Filtrar transações do Pipeimob no período por data_contrato (inclusivo)
    pipe_in_period, pipe_incomplete = filter_transactions_by_contract_date(pipeimob_transactions, start_iso, end_iso)
    
    # 2. Mapear negócios do Vista
    # REGRA: "Fechamento" é etapa, NÃO venda! Apenas status "Ganho" representa venda.
    # Negócios Ganho sem data de fechamento/ganho são isolados na seção de qualidade.
    vista_won_by_prop: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    vista_won_by_deal_id: Dict[str, Dict[str, Any]] = {}
    all_vista_won_in_period: List[Dict[str, Any]] = []
    vista_won_missing_gain_date_items: List[Dict[str, Any]] = []
    
    for deal in vista_deals:
        deal_id = str(deal.get("id") or deal.get("deal_id") or deal.get("codigo_negocio") or deal.get("codigo") or "").strip()
        status_norm = str(deal.get("status") or deal.get("Status") or "").strip().lower()
        etapa_norm = str(deal.get("etapa") or deal.get("Etapa") or deal.get("nome_etapa") or "").strip()
        
        # Ignorar se status não for Ganho (ex: Perdido ou Em aberto mesmo em etapa Fechamento)
        is_won = (status_norm == "ganho")
        
        prop_code = str(deal.get("codigo_imovel") or deal.get("CodigoImovel") or deal.get("property_code") or deal.get("imovel") or "").strip() or None
        deal_val = deal.get("valor") if deal.get("valor") is not None else deal.get("Valor")
        fech_date = deal.get("data_fechamento") or deal.get("DataFechamento") or deal.get("data_ganho") or deal.get("DataGanho")
        broker_name = str(deal.get("corretor_nome") or deal.get("CorretorNome") or deal.get("commercial_broker") or deal.get("NomeCorretor") or "").strip() or None
        fech_iso = parse_date_to_iso(fech_date)
        
        deal_record = {
            "deal_id": deal_id,
            "property_code": prop_code,
            "status": deal.get("Status") or deal.get("status") or "Ganho",
            "etapa": etapa_norm,
            "is_won": is_won,
            "valor": format_currency_decimal(deal_val),
            "data_fechamento": fech_iso,
            "commercial_broker": broker_name
        }
        
        if deal_id:
            vista_won_by_deal_id[deal_id] = deal_record

        if is_won:
            # Tratamento de negócios Ganho sem data de fechamento/ganho
            if not fech_iso:
                vista_won_missing_gain_date_items.append({
                    "vista_deal_id": deal_id or None,
                    "property_code": prop_code,
                    "commercial_broker": broker_name,
                    "vista_value": deal_record["valor"],
                    "status": deal_record["status"],
                    "reason": "Negócio com status Ganho no CRM Vista sem data de ganho/fechamento; excluído dos indicadores e conciliações do período por ausência de data."
                })
                # Não é incluído nos indicadores do período
                continue

            if prop_code:
                vista_won_by_prop[prop_code].append(deal_record)
                
            if fech_iso and start_iso and end_iso and start_iso <= fech_iso <= end_iso:
                all_vista_won_in_period.append(deal_record)
            elif not (start_iso and end_iso):
                all_vista_won_in_period.append(deal_record)

    reconciled_items = []
    matched_vista_deal_ids: Set[str] = set()
    
    matched_count = 0
    pipe_without_vista_count = 0
    value_mismatches_count = 0
    date_mismatches_count = 0
    ambiguities_count = 0
    missing_broker_count = 0
    
    total_vgv_dec = Decimal("0.00")
    total_vgc_dec = Decimal("0.00")
    
    # 3. Conciliar transações válidas do Pipeimob
    for p in pipe_in_period:
        issues = []
        prop_code = p["property_code"]
        
        # Somar VGV e VGC oficiais
        if p["official_value"] is not None:
            total_vgv_dec += Decimal(p["official_value"])
        if p["commission_value"] is not None:
            total_vgc_dec += Decimal(p["commission_value"])
            
        candidates = vista_won_by_prop.get(prop_code, []) if prop_code else []
        
        selected_deal = None
        is_ambiguous = False
        
        if len(candidates) == 1:
            selected_deal = candidates[0]
        elif len(candidates) > 1:
            # Desempate secundário por valor e data
            pipe_val = p["official_value"]
            pipe_date = p["official_sale_date"]
            
            # Filtro 1: candidatos com valor compatível (diferença <= 1.00)
            val_candidates = []
            for c in candidates:
                if c["valor"] is not None and pipe_val is not None:
                    diff = abs(Decimal(pipe_val) - Decimal(c["valor"]))
                    if diff <= Decimal("1.00"):
                        val_candidates.append(c)
                elif c["valor"] is None and pipe_val is None:
                    val_candidates.append(c)
                    
            if len(val_candidates) == 1:
                selected_deal = val_candidates[0]
            elif len(val_candidates) > 1:
                # Filtro 2: desempate por data exata
                date_candidates = [c for c in val_candidates if c["data_fechamento"] == pipe_date]
                if len(date_candidates) == 1:
                    selected_deal = date_candidates[0]
                else:
                    is_ambiguous = True
            else:
                is_ambiguous = True

        if selected_deal and not is_ambiguous:
            vista_val = selected_deal["valor"]
            comm_broker = selected_deal["commercial_broker"]
            vista_deal_id = selected_deal["deal_id"]
            vista_gain_date = selected_deal["data_fechamento"]
            if vista_deal_id:
                matched_vista_deal_ids.add(vista_deal_id)
            
            val_diff = "0.00"
            is_val_divergence = False
            if p["official_value"] is not None and vista_val is not None:
                diff = abs(Decimal(p["official_value"]) - Decimal(vista_val))
                val_diff = f"{diff:.2f}"
                if diff > Decimal("1.00"):
                    issues.append(f"Divergência de valor: Pipeimob={p['official_value']} vs Vista={vista_val}")
                    value_mismatches_count += 1
                    is_val_divergence = True
                    
            is_date_divergence = False
            if p["official_sale_date"] and vista_gain_date:
                if p["official_sale_date"] != vista_gain_date:
                    issues.append(f"Divergência de data: Pipeimob ({p['official_sale_date']}) vs Vista ({vista_gain_date})")
                    date_mismatches_count += 1
                    is_date_divergence = True
                    
            if not comm_broker:
                missing_broker_count += 1
                issues.append("Corretor comercial não identificado no CRM Vista")
                
            status = "CONCILIADO"
            if is_val_divergence:
                status = "DIVERGENCIA_VALOR"
            elif is_date_divergence:
                status = "DIVERGENCIA_DATA"
                
            if status == "CONCILIADO":
                matched_count += 1
                
            delay_days = None
            if p["official_sale_date"] and p["ccv_upload_date"]:
                try:
                    d_sale = date.fromisoformat(p["official_sale_date"])
                    d_up = date.fromisoformat(p["ccv_upload_date"])
                    delay_days = (d_up - d_sale).days
                except Exception:
                    pass

            reconciled_items.append({
                "status": status,
                "issues": issues,
                "pipeimob_transaction_id": p["pipeimob_transaction_id"],
                "pipeimob_contract_code": p["pipeimob_contract_code"],
                "vista_deal_id": vista_deal_id,
                "property_code": prop_code,
                "official_sale_date": p["official_sale_date"],
                "ccv_signature_date": p["ccv_signature_date"],
                "ccv_upload_date": p["ccv_upload_date"],
                "vista_gain_date": vista_gain_date,
                "delay_days": delay_days,
                "official_value": p["official_value"],
                "commission_value": p["commission_value"],
                "commission_date": p["commission_date"],
                "property_address": p["property_address"],
                "vista_value": vista_val,
                "value_difference": val_diff,
                "commercial_broker": comm_broker,
                "pipeimob_manager": p["pipeimob_manager"],
                "fiscal_broker": None,
                "manager_and_broker_differ": bool(p["pipeimob_manager"] and comm_broker and p["pipeimob_manager"] != comm_broker),
                "broker_roles_differ": False,
                "missing_fields": p["missing_fields"],
                # Aliases
                "ccv_assinatura": p["ccv_signature_date"],
                "subida_ccv": p["ccv_upload_date"],
                "vgv": p["official_value"],
                "vgc": p["commission_value"],
                "endereco": p["property_address"],
                "gerente_pipeimob": p["pipeimob_manager"]
            })

        elif is_ambiguous:
            # Ambiguidade: múltiplos ganhos não resolvidos por critérios secundários
            ambiguities_count += 1
            missing_broker_count += 1
            candidate_ids = [c["deal_id"] for c in candidates if c.get("deal_id")]
            issues.append(
                f"Ambiguidade: {len(candidates)} negócios Ganho encontrados no CRM Vista para o imóvel {prop_code} (Negócios: {', '.join(candidate_ids)})"
            )
            
            reconciled_items.append({
                "status": "AMBIGUIDADE_MULTIPLOS_GANHOS_VISTA",
                "issues": issues,
                "pipeimob_transaction_id": p["pipeimob_transaction_id"],
                "pipeimob_contract_code": p["pipeimob_contract_code"],
                "vista_deal_id": None,
                "property_code": prop_code,
                "official_sale_date": p["official_sale_date"],
                "ccv_signature_date": p["ccv_signature_date"],
                "ccv_upload_date": p["ccv_upload_date"],
                "vista_gain_date": None,
                "delay_days": None,
                "official_value": p["official_value"],
                "commission_value": p["commission_value"],
                "commission_date": p["commission_date"],
                "property_address": p["property_address"],
                "vista_value": None,
                "value_difference": None,
                "commercial_broker": None,
                "pipeimob_manager": p["pipeimob_manager"],
                "fiscal_broker": None,
                "manager_and_broker_differ": None,
                "broker_roles_differ": False,
                "missing_fields": p["missing_fields"],
                "candidates_count": len(candidates),
                "candidate_deal_ids": candidate_ids,
                # Aliases
                "ccv_assinatura": p["ccv_signature_date"],
                "subida_ccv": p["ccv_upload_date"],
                "vgv": p["official_value"],
                "vgc": p["commission_value"],
                "endereco": p["property_address"],
                "gerente_pipeimob": p["pipeimob_manager"]
            })

        else:
            # Pipeimob sem ganho no Vista
            pipe_without_vista_count += 1
            missing_broker_count += 1
            issues.append("Venda confirmada no Pipeimob sem negócio Ganho correspondente no CRM Vista")
            
            reconciled_items.append({
                "status": "PIPEIMOB_SEM_GANHO_VISTA",
                "issues": issues,
                "pipeimob_transaction_id": p["pipeimob_transaction_id"],
                "pipeimob_contract_code": p["pipeimob_contract_code"],
                "vista_deal_id": None,
                "property_code": prop_code,
                "official_sale_date": p["official_sale_date"],
                "ccv_signature_date": p["ccv_signature_date"],
                "ccv_upload_date": p["ccv_upload_date"],
                "vista_gain_date": None,
                "delay_days": None,
                "official_value": p["official_value"],
                "commission_value": p["commission_value"],
                "commission_date": p["commission_date"],
                "property_address": p["property_address"],
                "vista_value": None,
                "value_difference": None,
                "commercial_broker": None,
                "pipeimob_manager": p["pipeimob_manager"],
                "fiscal_broker": None,
                "manager_and_broker_differ": None,
                "broker_roles_differ": False,
                "missing_fields": p["missing_fields"],
                # Aliases
                "ccv_assinatura": p["ccv_signature_date"],
                "subida_ccv": p["ccv_upload_date"],
                "vgv": p["official_value"],
                "vgc": p["commission_value"],
                "endereco": p["property_address"],
                "gerente_pipeimob": p["pipeimob_manager"]
            })

    # 4. Itens com dados incompletos (ex: falta de data_contrato)
    for p in pipe_incomplete:
        reconciled_items.append({
            "status": "DADO_FONTE_INCOMPLETO",
            "issues": ["data_contrato ausente na API Pipeimob V2"],
            "pipeimob_transaction_id": p["pipeimob_transaction_id"],
            "pipeimob_contract_code": p["pipeimob_contract_code"],
            "vista_deal_id": None,
            "property_code": p["property_code"],
            "official_sale_date": None,
            "ccv_signature_date": None,
            "ccv_upload_date": p["ccv_upload_date"],
            "vista_gain_date": None,
            "delay_days": None,
            "official_value": p["official_value"],
            "commission_value": p["commission_value"],
            "commission_date": p["commission_date"],
            "property_address": p["property_address"],
            "vista_value": None,
            "value_difference": None,
            "commercial_broker": None,
            "pipeimob_manager": p["pipeimob_manager"],
            "fiscal_broker": None,
            "manager_and_broker_differ": None,
            "broker_roles_differ": False,
            "missing_fields": p["missing_fields"],
            # Aliases
            "ccv_assinatura": None,
            "subida_ccv": p["ccv_upload_date"],
            "vgv": p["official_value"],
            "vgc": p["commission_value"],
            "endereco": p["property_address"],
            "gerente_pipeimob": p["pipeimob_manager"]
        })

    # 5. Vendas exclusivas do Vista (Ganho no Vista no período sem contrato correspondente no Pipeimob)
    vista_without_pipeimob_count = 0
    for v in all_vista_won_in_period:
        v_id = v.get("deal_id")
        if v_id and v_id not in matched_vista_deal_ids:
            vista_without_pipeimob_count += 1
            reconciled_items.append({
                "status": "VISTA_SEM_CONTRATO_PIPEIMOB",
                "issues": ["Negócio com status Ganho no CRM Vista sem contrato correspondente no Pipeimob V2"],
                "pipeimob_transaction_id": None,
                "pipeimob_contract_code": None,
                "vista_deal_id": v_id,
                "property_code": v.get("property_code"),
                "official_sale_date": None,
                "ccv_signature_date": None,
                "ccv_upload_date": None,
                "vista_gain_date": v.get("data_fechamento"),
                "delay_days": None,
                "official_value": None,
                "commission_value": None,
                "commission_date": None,
                "property_address": None,
                "vista_value": v.get("valor"),
                "value_difference": None,
                "commercial_broker": v.get("commercial_broker"),
                "pipeimob_manager": None,
                "fiscal_broker": None,
                "manager_and_broker_differ": None,
                "broker_roles_differ": False,
                "missing_fields": ["pipeimob_contract"],
                # Aliases
                "ccv_assinatura": None,
                "subida_ccv": None,
                "vgv": None,
                "vgc": None,
                "endereco": None,
                "gerente_pipeimob": None
            })

    official_sales_count = len(pipe_in_period)
    
    summary = {
        "official_sales": official_sales_count,
        "official_vgv": f"{total_vgv_dec:.2f}",
        "official_vgc": f"{total_vgc_dec:.2f}",
        "matched": matched_count,
        "pipeimob_without_vista_gain": pipe_without_vista_count,
        "vista_without_pipeimob_contract": vista_without_pipeimob_count,
        "value_mismatches": value_mismatches_count,
        "date_mismatches": date_mismatches_count,
        "no_automatic_link": 0,
        "source_data_incomplete": len(pipe_incomplete),
        "ambiguous_vista_deals": ambiguities_count,
        "missing_commercial_broker": missing_broker_count,
        "vista_won_missing_gain_date": len(vista_won_missing_gain_date_items)
    }
    
    return {
        "contract_version": "director-sales-reconciliation-v2.0",
        "official_source": "Pipeimob V2 (api.pipeimob.com.br via pipeimob-report)",
        "commercial_source": "CRM Vista (api.vistahost.com.br)",
        "period": {
            "start": start_iso,
            "end": end_iso,
            "filter_field": "data_contrato",
            "inclusive_bounds": True
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "data_quality": {
            "vista_won_missing_gain_date_count": len(vista_won_missing_gain_date_items),
            "vista_won_missing_gain_date_items": vista_won_missing_gain_date_items,
            "documentation": "Registros com status Ganho no CRM Vista que não possuem data de ganho/fechamento são expressamente excluídos dos indicadores, conciliações e pendências do período por falta de data para enquadramento temporal."
        },
        "items": reconciled_items
    }
