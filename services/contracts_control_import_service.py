import os
import uuid
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session

from models.contracts_control import (
    ContractsControlImportPreview,
    ContractsControlImportPreviewItem,
    normalize_responsible_name
)
from repositories.contracts_control_import_repository import ContractsControlImportRepository
from repositories.contracts_control_repository import ContractsControlRepository
from services.contracts_control_import_parser import ContractsControlImportParser
from services.contracts_control_import_matcher import ContractsControlImportMatcher, normalize_text

EXPLICIT_OVERRIDES = {
    "39177": "Guilherme",
    "42623": "Guilherme",
    "42625": "Guilherme"
}

PARSER_VERSION = "1.0"

class ContractsControlImportService:
    @staticmethod
    async def create_import_preview(
        db: Session,
        file_path: str,
        filename: str,
        created_by_sub: str,
        dataset: List[Dict[str, Any]]
    ) -> ContractsControlImportPreview:
        
        # 1. Clean up expired previews
        now_dt = datetime.now(timezone.utc)
        ContractsControlImportRepository.delete_expired_previews(db, now_dt)

        # 2. Parse file
        parsed_rows = ContractsControlImportParser.parse_file(file_path, filename)
        
        # 3. Calculate source_hash of normalized contents
        # Sort parsed rows semantically to ensure deterministic hashing
        normalized_data_for_hash = []
        for r in parsed_rows:
            normalized_data_for_hash.append({
                "aba": r.get("aba"),
                "linha": r.get("linha"),
                "codigo_imovel": r.get("codigo_imovel"),
                "responsavel_planilha": r.get("responsavel_planilha"),
                "gerente": r.get("gerente"),
                "nome_imovel": r.get("nome_imovel"),
                "data_cadastro": r.get("data_cadastro"),
                "data_assinatura_ccv": r.get("data_assinatura_ccv")
            })
            
        sorted_for_hash = sorted(
            normalized_data_for_hash,
            key=lambda x: (x.get("aba") or "", x.get("linha") or 0, x.get("codigo_imovel") or "")
        )
        serialized = json.dumps(sorted_for_hash, sort_keys=True)
        source_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

        # Group rows by property code to detect duplicates/conflicts
        grouped_rows = {}
        invalid_source_rows = []
        
        for r in parsed_rows:
            code = r.get("codigo_imovel")
            if not code:
                # Row without code is invalid
                invalid_source_rows.append({
                    "aba": r.get("aba"),
                    "linha": r.get("linha"),
                    "codigo_imovel": None,
                    "nome_imovel": r.get("nome_imovel"),
                    "responsavel_planilha": r.get("responsavel_planilha"),
                    "decisao_proposta": "invalid_source_row",
                    "motivo": "Código do imóvel vazio ou inválido."
                })
                continue
            grouped_rows.setdefault(code, []).append(r)

        # Pre-load all registered responsibles
        db_responsibles = ContractsControlRepository.list_active_responsibles(db, include_inactive=True)
        responsibles_map = {normalize_responsible_name(r.name): r for r in db_responsibles}

        # Index transactions by property code
        transactions_by_code = {}
        for tx in dataset:
            code = tx.get("codigo_imovel")
            if code:
                transactions_by_code.setdefault(str(code), []).append(tx)

        # Pre-load manual attributions to prevent queries inside loop
        all_tx_ids = []
        for tx_list in transactions_by_code.values():
            for tx in tx_list:
                tid = tx.get("transacao_unique_id_pipeimob")
                if tid:
                    all_tx_ids.append(tid)
                    
        db_manual_data = ContractsControlRepository.get_manual_data_by_transaction_ids(db, all_tx_ids)
        manual_data_map = {md.transaction_id: md for md in db_manual_data}

        # Initialize summary counts
        summary = {
            "source_rows_count": len(parsed_rows),
            "unique_property_codes_count": len(grouped_rows),
            "rows_with_responsible_count": 0,
            "rows_without_responsible_count": 0,
            "duplicate_same_value_count": 0,
            "duplicate_conflict_count": 0,
            "unique_match_count": 0,
            "ambiguous_match_count": 0,
            "not_found_count": 0,
            "responsible_not_registered_count": 0,
            "already_synchronized_count": 0,
            "to_assign_count": 0,
            "to_change_count": 0,
            "to_clear_count": 0,
            "invalid_source_row_count": 0
        }

        preview_id = uuid.uuid4()
        preview_items = []

        # Process invalid rows first (without code)
        for ir in invalid_source_rows:
            summary["invalid_source_row_count"] += 1
            if ir["responsavel_planilha"]:
                summary["rows_with_responsible_count"] += 1
            else:
                summary["rows_without_responsible_count"] += 1
                
            preview_items.append(ContractsControlImportPreviewItem(
                id=uuid.uuid4(),
                preview_id=preview_id,
                aba=ir["aba"],
                linha=ir["linha"],
                codigo_imovel=ir["codigo_imovel"],
                nome_imovel=ir["nome_imovel"],
                responsavel_planilha=ir["responsavel_planilha"],
                responsavel_atual_secretaria=None,
                transaction_id=None,
                versao_manual_atual=None,
                decisao_proposta=ir["decisao_proposta"],
                motivo=ir["motivo"],
                source_occurrences=None
            ))

        # Process each unique property code
        for code, occurrences in grouped_rows.items():
            # Counts for source responsibles
            for o in occurrences:
                if o["responsavel_planilha"]:
                    summary["rows_with_responsible_count"] += 1
                else:
                    summary["rows_without_responsible_count"] += 1

            # Consolidate occurrences
            source_occurrences = {
                "occurrences": [
                    {
                        "aba": o["aba"],
                        "linha": o["linha"],
                        "responsavel": o["responsavel_planilha"],
                        "gerente": o["gerente"]
                    } for o in occurrences
                ]
            }

            # Check duplicate conflict rules
            is_conflict = False
            target_resp_name = None
            
            if len(occurrences) > 1:
                # Get unique responsibles (non-empty)
                resps_present = [o["responsavel_planilha"] for o in occurrences if o["responsavel_planilha"]]
                unique_resps_norm = list(set(normalize_responsible_name(r) for r in resps_present))
                has_empty = any(o["responsavel_planilha"] is None for o in occurrences)
                
                if code in EXPLICIT_OVERRIDES:
                    target_resp_name = EXPLICIT_OVERRIDES[code]
                    summary["duplicate_conflict_count"] += 1 # Pre-override conflict is counted
                else:
                    # Duplicate same value: no conflict
                    if len(unique_resps_norm) == 1 and not has_empty:
                        target_resp_name = resps_present[0]
                        summary["duplicate_same_value_count"] += len(occurrences) - 1
                    # Empty and filled mix is a conflict
                    elif (len(unique_resps_norm) > 0 and has_empty) or len(unique_resps_norm) > 1:
                        is_conflict = True
                        summary["duplicate_conflict_count"] += len(occurrences) - 1
                    else:
                        # All empty
                        target_resp_name = None
                        summary["duplicate_same_value_count"] += len(occurrences) - 1
            else:
                target_resp_name = occurrences[0]["responsavel_planilha"]

            # Main representative row details
            main_row = occurrences[0]
            aba = main_row["aba"]
            linha = main_row["linha"]
            nome_imovel = main_row["nome_imovel"]
            gerente = main_row["gerente"]

            # Matching and decision mapping
            decisao_proposta = "invalid_source_row"
            motivo = None
            transaction_id = None
            versao_manual_atual = None
            responsavel_atual_secretaria = None

            # 1. Ana Cristina check (must fail with invalid_source_row)
            norm_target_resp = normalize_responsible_name(target_resp_name) if target_resp_name else ""
            if norm_target_resp == "ana cristina":
                decisao_proposta = "invalid_source_row"
                motivo = "Responsável 'Ana Cristina' deve ser ignorado na importação oficial."
                summary["invalid_source_row_count"] += 1
            
            # 2. Source Conflict check
            elif is_conflict:
                decisao_proposta = "source_conflict"
                motivo = "Conflito de responsabilidade entre linhas duplicadas da planilha."
                summary["duplicate_conflict_count"] += 1
                
            else:
                # 3. Match against Pipeimob
                tx_candidates = transactions_by_code.get(str(code), [])
                matched_tx, match_status, match_reason = ContractsControlImportMatcher.match_transaction(
                    codigo_imovel=code,
                    transactions_by_code=tx_candidates,
                    gerente=gerente,
                    nome_imovel=nome_imovel,
                    data_cadastro=main_row["data_cadastro"],
                    data_assinatura_ccv=main_row["data_assinatura_ccv"]
                )

                if match_status == "not_found":
                    decisao_proposta = "not_found"
                    motivo = match_reason
                    summary["not_found_count"] += 1
                elif match_status == "ambiguous_match":
                    decisao_proposta = "ambiguous_match"
                    motivo = match_reason
                    summary["ambiguous_match_count"] += 1
                else:
                    # Unique Match found!
                    summary["unique_match_count"] += 1
                    transaction_id = matched_tx.get("transacao_unique_id_pipeimob")
                    
                    # Fetch current manual attribution
                    current_md = manual_data_map.get(transaction_id)
                    if current_md:
                        versao_manual_atual = current_md.version
                        if current_md.responsible:
                            responsavel_atual_secretaria = current_md.responsible.name
                    
                    # 4. Responsible Validation
                    if not target_resp_name:
                        # Empty responsible -> Propose Ready to Clear or Already Synchronized
                        if responsavel_atual_secretaria:
                            decisao_proposta = "ready_to_clear"
                            motivo = "Responsável em branco na planilha; remover atribuição atual da Secretaria."
                            summary["to_clear_count"] += 1
                        else:
                            decisao_proposta = "already_synchronized"
                            motivo = "Sem responsável na planilha e na Secretaria (sincronizado)."
                            summary["already_synchronized_count"] += 1
                    else:
                        # Validate against registered responsibles
                        resp_entity = responsibles_map.get(norm_target_resp)
                        if not resp_entity:
                            decisao_proposta = "responsible_not_registered"
                            motivo = f"Responsável '{target_resp_name}' não cadastrado no sistema."
                            summary["responsible_not_registered_count"] += 1
                        elif not resp_entity.active:
                            decisao_proposta = "responsible_inactive"
                            motivo = f"Responsável '{target_resp_name}' está inativo no sistema."
                            summary["invalid_source_row_count"] += 1  # Inactive counts as invalid row constraint
                        else:
                            # Valid Active Responsible!
                            norm_current_resp = normalize_responsible_name(responsavel_atual_secretaria) if responsavel_atual_secretaria else ""
                            if norm_target_resp == norm_current_resp:
                                decisao_proposta = "already_synchronized"
                                motivo = f"Responsável '{target_resp_name}' já está atribuído corretamente."
                                summary["already_synchronized_count"] += 1
                            else:
                                if responsavel_atual_secretaria:
                                    decisao_proposta = "ready_to_change"
                                    motivo = f"Alterar responsável de '{responsavel_atual_secretaria}' para '{resp_entity.name}'."
                                    summary["to_change_count"] += 1
                                else:
                                    decisao_proposta = "ready_to_assign"
                                    motivo = f"Atribuir novo responsável '{resp_entity.name}'."
                                    summary["to_assign_count"] += 1

            # Append consolidated item
            preview_items.append(ContractsControlImportPreviewItem(
                id=uuid.uuid4(),
                preview_id=preview_id,
                aba=aba,
                linha=linha,
                codigo_imovel=code,
                nome_imovel=nome_imovel,
                responsavel_planilha=target_resp_name,
                responsavel_atual_secretaria=responsavel_atual_secretaria,
                transaction_id=transaction_id,
                versao_manual_atual=versao_manual_atual,
                decisao_proposta=decisao_proposta,
                motivo=motivo,
                source_occurrences=source_occurrences
            ))

        # Save to database
        fmt = "xlsx" if filename.lower().endswith(".xlsx") else "csv"
        preview = ContractsControlImportPreview(
            id=preview_id,
            source_filename=os.path.basename(filename),
            source_format=fmt,
            parser_version=PARSER_VERSION,
            created_by_sub=created_by_sub,
            status="ready",
            source_hash=source_hash,
            summary=summary,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24)
        )
        
        ContractsControlImportRepository.create_preview(db, preview)
        ContractsControlImportRepository.create_preview_items(db, preview_items)
        
        return preview
