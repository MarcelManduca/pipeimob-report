#!/usr/bin/env python3
"""
scripts/import_responsibles_once.py

Rotina CLI de carga única de responsáveis da Secretaria de Vendas a partir da planilha oficial 2026.
Garante:
- Execução isolada por linha de comando (sem endpoints públicos ou frontend).
- Simulação por padrão (--dry-run sem especificação de --apply).
- Checagem estrita de APP_ENV e hash SHA-256 no --apply.
- Transação atômica única com rollback integral em caso de erro.
"""

import sys
import os
import argparse
import csv
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set

# Add parent directory to sys.path if running as script inside scripts/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database
from repositories.contracts_control_repository import ContractsControlRepository
from models.contracts_control import (
    ContractsControlResponsible,
    ContractsControlManualData,
    normalize_responsible_name,
)

VALID_RESPONSIBLES = {"Guilherme", "Cristina", "Carol", "Laise"}
EXPLICIT_GUILHERME_CODES = {"39177", "42623", "42625"}
ALLOWED_APP_ENVS = {"staging", "production", "development", "test"}


def calculate_file_sha256(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalize_code(raw_code: str) -> str:
    if not raw_code:
        return ""
    code = raw_code.strip().split(".")[0].strip()
    return code


def parse_consolidated_csv(file_path: str) -> Dict[str, Any]:
    """
    Lê e valida o arquivo consolidado CSV.
    Retorna métricas brutas e dicionário de propostas por código de imóvel.
    """
    rows_read = 0
    rows_with_responsible = 0
    rows_empty_responsible = 0

    ana_cristina_ignored: List[Dict[str, Any]] = []
    invalid_responsible_ignored: List[Dict[str, Any]] = []
    explicit_overrides_applied: List[Dict[str, Any]] = []

    # Map code -> list of occurrences
    code_occurrences: Dict[str, List[Dict[str, Any]]] = {}
    all_unique_codes: Set[str] = set()

    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_read += 1
            sheet = row.get("source_sheet", "").strip()
            source_row = int(row.get("source_row", 0)) if row.get("source_row") else 0
            code = normalize_code(row.get("codigo_imovel", ""))
            raw_resp = row.get("responsavel", "").strip()
            gerente = row.get("gerente", "").strip()

            if code:
                all_unique_codes.add(code)

            if not raw_resp and code not in EXPLICIT_GUILHERME_CODES:
                rows_empty_responsible += 1
                continue

            rows_with_responsible += 1

            final_resp = raw_resp

            # Check explicit override rule for 39177, 42623, 42625
            if code in EXPLICIT_GUILHERME_CODES:
                final_resp = "Guilherme"
                explicit_overrides_applied.append({
                    "source_sheet": sheet,
                    "source_row": source_row,
                    "codigo_imovel": code,
                    "original_responsavel": raw_resp,
                    "assigned_responsavel": "Guilherme"
                })

            if raw_resp.lower() == "ana cristina":
                ana_cristina_ignored.append({
                    "source_sheet": sheet,
                    "source_row": source_row,
                    "codigo_imovel": code,
                    "raw_responsavel": raw_resp,
                    "reason": "Ana Cristina e invalida - nao converter para Cristina"
                })
                continue

            if final_resp not in VALID_RESPONSIBLES:
                invalid_responsible_ignored.append({
                    "source_sheet": sheet,
                    "source_row": source_row,
                    "codigo_imovel": code,
                    "raw_responsavel": raw_resp,
                    "reason": "Responsavel nao cadastrado/invalido"
                })
                continue

            if code not in code_occurrences:
                code_occurrences[code] = []
            code_occurrences[code].append({
                "sheet": sheet,
                "source_row": source_row,
                "responsavel": final_resp,
                "gerente": gerente
            })

    # Consolidate by code
    proposed_by_code: Dict[str, str] = {}
    conflict_codes: List[Dict[str, Any]] = []
    duplicate_same_resp_count = 0

    for code, occs in code_occurrences.items():
        resps = set(o["responsavel"] for o in occs)
        if len(occs) > 1:
            duplicate_same_resp_count += (len(occs) - 1)

        if len(resps) == 1:
            proposed_by_code[code] = list(resps)[0]
        else:
            conflict_codes.append({
                "codigo_imovel": code,
                "distinct_responsibles": list(resps),
                "occurrences": occs
            })

    return {
        "source_counts": {
            "total_data_rows": rows_read,
            "rows_with_responsible": rows_with_responsible,
            "empty_responsible_rows": rows_empty_responsible,
            "unique_codes_all_rows": len(all_unique_codes),
            "unique_codes_with_responsible": len(code_occurrences),
            "duplicate_occurrences": duplicate_same_resp_count,
            "duplicate_codes_same_responsible_count": duplicate_same_resp_count,
            "duplicate_codes_different_responsible_count": len(conflict_codes)
        },
        "proposed_by_code": proposed_by_code,
        "conflict_codes": conflict_codes,
        "ana_cristina_ignored": ana_cristina_ignored,
        "invalid_responsible_ignored": invalid_responsible_ignored,
        "explicit_overrides_applied": explicit_overrides_applied
    }


def main():
    parser = argparse.ArgumentParser(description="Rotina CLI de Carga Única de Responsáveis (Secretaria de Vendas)")
    parser.add_argument("--file", required=True, help="Caminho do arquivo CSV consolidado")
    parser.add_argument("--target", required=True, choices=["staging", "production"], help="Ambiente alvo (staging ou production)")
    parser.add_argument("--apply", action="store_true", help="Executa a escrita no banco de dados. Sem esta flag, executa apenas dry-run.")
    parser.add_argument("--confirm-production", action="store_true", help="Confirmação necessária para apply em produção")
    parser.add_argument("--expected-source-sha256", help="SHA-256 esperado do arquivo consolidado (obrigatório para --apply)")

    args = parser.parse_args()

    # 1. Validar existência do arquivo
    if not os.path.isfile(args.file):
        print(f"ERRO: Arquivo '{args.file}' não encontrado.", file=sys.stderr)
        sys.exit(1)

    # 2. Validar APP_ENV do sistema
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if not app_env or app_env not in ALLOWED_APP_ENVS:
        print(f"ABORTADO: APP_ENV ausente, vazio ou desconhecido: '{app_env}'. Esperado: staging, production, development ou test.", file=sys.stderr)
        sys.exit(1)

    # 3. Validar coincidência de target com APP_ENV
    if args.target == "staging" and app_env not in ("staging", "development", "test"):
        print(f"ABORTADO: --target staging especificado, mas APP_ENV real é '{app_env}'.", file=sys.stderr)
        sys.exit(1)

    if args.target == "production" and app_env != "production":
        print(f"ABORTADO: --target production especificado, mas APP_ENV real é '{app_env}'.", file=sys.stderr)
        sys.exit(1)

    # 4. Calcular SHA-256 do arquivo
    file_sha256 = calculate_file_sha256(args.file)

    # 5. Se for --apply, exigir validações de segurança
    if args.apply:
        if not args.expected_source_sha256:
            print("ABORTADO: A flag --expected-source-sha256 é OBRIGATÓRIA para execução com --apply.", file=sys.stderr)
            sys.exit(1)

        if args.expected_source_sha256.strip().lower() != file_sha256.lower():
            print(f"ABORTADO: Hash SHA-256 do arquivo ({file_sha256}) difere do esperado ({args.expected_source_sha256}).", file=sys.stderr)
            sys.exit(1)

        if args.target == "production" and not args.confirm_production:
            print("ABORTADO: Execuções com --apply em production exigem a flag --confirm-production.", file=sys.stderr)
            sys.exit(1)

    execution_id = f"exec_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # 6. Realizar parsing e consolidação em memória
    parsed = parse_consolidated_csv(args.file)
    proposed_by_code = parsed["proposed_by_code"]

    # 7. Matching com dataset do Pipeimob (Antes de abrir qualquer transação de escrita)
    if not database.SessionLocal:
        print("ABORTADO: database.SessionLocal não está inicializado.", file=sys.stderr)
        sys.exit(1)

    db = database.SessionLocal()
    try:
        # Map responsible names to UUIDs in DB
        resp_name_to_id: Dict[str, uuid.UUID] = {}
        for r_name in VALID_RESPONSIBLES:
            resp_obj = ContractsControlRepository.get_responsible_by_normalized_name(db, normalize_responsible_name(r_name))
            if resp_obj and resp_obj.active:
                resp_name_to_id[r_name] = resp_obj.id

        unique_codes_found = 0
        unique_codes_not_found = 0
        unique_codes_ambiguous = 0

        eligible_items: List[Dict[str, Any]] = []
        pending_items: List[Dict[str, Any]] = []

        # Add conflicts from parsing to pending_items
        for conf in parsed["conflict_codes"]:
            pending_items.append({
                "codigo_imovel": conf["codigo_imovel"],
                "reason": "conflict_different_responsibles",
                "details": conf["distinct_responsibles"]
            })

        for code, target_resp in proposed_by_code.items():
            resp_id = resp_name_to_id.get(target_resp)
            if not resp_id:
                pending_items.append({
                    "codigo_imovel": code,
                    "reason": "responsible_not_registered_in_db",
                    "responsible": target_resp
                })
                continue

            # Query deals matching this code
            deals = ContractsControlRepository.list_deals_by_property_code(db, code)
            
            if len(deals) == 0:
                unique_codes_not_found += 1
                pending_items.append({
                    "codigo_imovel": code,
                    "reason": "code_not_found_in_pipeimob"
                })
            elif len(deals) > 1:
                unique_codes_ambiguous += 1
                pending_items.append({
                    "codigo_imovel": code,
                    "reason": "ambiguous_code_match",
                    "deals_count": len(deals)
                })
            else:
                unique_codes_found += 1
                deal = deals[0]
                tx_id = deal["transaction_id"]
                eligible_items.append({
                    "codigo_imovel": code,
                    "transaction_id": tx_id,
                    "target_responsible": target_resp,
                    "target_responsible_id": resp_id
                })

        already_synchronized_count = 0
        new_assignments_count = 0
        responsible_changes_count = 0

        # Classify eligible items against current DB manual data
        items_to_apply: List[Dict[str, Any]] = []

        for item in eligible_items:
            tx_id = item["transaction_id"]
            target_resp_id = item["target_responsible_id"]
            md = ContractsControlRepository.get_manual_data_by_transaction_id(db, tx_id)

            if md and md.responsible_id == target_resp_id:
                already_synchronized_count += 1
            else:
                if md is None:
                    new_assignments_count += 1
                    item["pre_existing"] = False
                    item["previous_responsible_id"] = None
                    item["previous_version"] = 0
                else:
                    responsible_changes_count += 1
                    item["pre_existing"] = True
                    item["previous_responsible_id"] = md.responsible_id
                    item["previous_version"] = md.version

                items_to_apply.append(item)

        db.close() # Close read-only session before write decision

        report_metrics = {
            "source_file_sha256": file_sha256,
            "execution_id": execution_id,
            "mode": "apply" if args.apply else "dry_run",
            "target_environment": args.target,
            "app_env_real": app_env,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "source_counts": parsed["source_counts"],
            "database_matching_results": {
                "unique_codes_found": unique_codes_found,
                "unique_codes_not_found": unique_codes_not_found,
                "unique_codes_ambiguous": unique_codes_ambiguous,
                "deals_eligible": len(eligible_items),
                "already_synchronized": already_synchronized_count,
                "new_assignments": new_assignments_count,
                "responsible_changes": responsible_changes_count,
                "items_to_write_count": len(items_to_apply)
            },
            "proposed_unique_codes_by_responsible": {
                r: sum(1 for c, resp in proposed_by_code.items() if resp == r) for r in VALID_RESPONSIBLES
            }
        }

        # 8. Execução com --apply (Transação Atômica Única)
        applied_count = 0
        failed_count = 0
        actor_sub = f"official_spreadsheet_2026_one_time:{execution_id}"

        if args.apply and items_to_apply:
            # Create backup dir
            backup_dir = os.path.join("backups", f"import_once_{execution_id}")
            os.makedirs(backup_dir, exist_ok=True)
            
            # Save pre-apply snapshot backup
            snapshot_backup = [
                {
                    "transaction_id": it["transaction_id"],
                    "codigo_imovel": it["codigo_imovel"],
                    "pre_existing": it["pre_existing"],
                    "previous_responsible_id": str(it["previous_responsible_id"]) if it["previous_responsible_id"] else None,
                    "previous_version": it["previous_version"],
                    "applied_responsible_id": str(it["target_responsible_id"]),
                    "applied_version": it["previous_version"] + 1
                }
                for it in items_to_apply
            ]
            with open(os.path.join(backup_dir, "backup_pre_import.json"), "w", encoding="utf-8") as f:
                json.dump(snapshot_backup, f, indent=2)

            write_db = database.SessionLocal()
            try:
                for it in items_to_apply:
                    tx_id = it["transaction_id"]
                    resp_id = it["target_responsible_id"]
                    pre_existing = it["pre_existing"]

                    if not pre_existing:
                        # Create initial manual data
                        md = ContractsControlRepository.create_manual_data(
                            write_db, tx_id, resp_id, actor_sub
                        )
                        ContractsControlRepository.create_history_record(
                            write_db, tx_id, "responsible_id", None, str(resp_id), 0, 1, actor_sub
                        )
                    else:
                        prev_ver = it["previous_version"]
                        prev_resp_id = it["previous_responsible_id"]
                        success = ContractsControlRepository.update_manual_data_optimistic(
                            write_db, tx_id, resp_id, prev_ver, actor_sub
                        )
                        if not success:
                            raise ValueError(f"Optimistic lock conflict on transaction_id {tx_id}")
                        
                        ContractsControlRepository.create_history_record(
                            write_db,
                            tx_id,
                            "responsible_id",
                            str(prev_resp_id) if prev_resp_id else None,
                            str(resp_id),
                            prev_ver,
                            prev_ver + 1,
                            actor_sub
                        )
                    
                    applied_count += 1
                
                # Commit ALL changes in a single atomic commit
                write_db.commit()
                print(f"SUCESSO: Transação atômica concluída. {applied_count} alterações aplicadas com sucesso.")
            except Exception as e:
                write_db.rollback()
                failed_count = len(items_to_apply)
                applied_count = 0
                print(f"ERRO CRÍTICO NA TRANSAÇÃO: Rollback integral executado. Causa: {e}", file=sys.stderr)
                raise
            finally:
                write_db.close()

        # 9. Salvar relatórios
        reports_dir = os.path.join("reports", f"import_once_{execution_id}")
        os.makedirs(reports_dir, exist_ok=True)

        full_report = {
            "summary": report_metrics,
            "application_results": {
                "applied_count": applied_count,
                "failed_count": failed_count
            },
            "pending_items": pending_items,
            "ana_cristina_ignored": parsed["ana_cristina_ignored"],
            "explicit_overrides_applied": parsed["explicit_overrides_applied"]
        }

        with open(os.path.join(reports_dir, "report.json"), "w", encoding="utf-8") as f:
            json.dump(full_report, f, indent=2, ensure_ascii=False)

        # Output readable summary
        print("=" * 70)
        print(f"RELATÓRIO DE CARGA ÚNICA — EXECUTION ID: {execution_id}")
        print("=" * 70)
        print(f"Modo: {'APPLY (Escrita)' if args.apply else 'DRY-RUN (Simulação)'}")
        print(f"Target: {args.target} (APP_ENV: {app_env})")
        print(f"Hash SHA-256 do arquivo: {file_sha256}")
        print("-" * 70)
        print("CONTAGENS DA FONTE:")
        print(f"  Total de linhas lidas: {parsed['source_counts']['total_data_rows']}")
        print(f"  Linhas com responsável: {parsed['source_counts']['rows_with_responsible']}")
        print(f"  Linhas vazias ignoradas: {parsed['source_counts']['empty_responsible_rows']}")
        print(f"  Códigos únicos (linhas c/ resp): {parsed['source_counts']['unique_codes_with_responsible']}")
        print(f"  Duplicidades consolidadas: {parsed['source_counts']['duplicate_occurrences']}")
        print("-" * 70)
        print("RESULTADOS DE MATCHING PIPEIMOB (BANCO):")
        print(f"  Códigos encontrados (elegíveis): {unique_codes_found}")
        print(f"  Códigos não encontrados no Pipeimob: {unique_codes_not_found}")
        print(f"  Códigos ambíguos (>1 negócio): {unique_codes_ambiguous}")
        print(f"  Já sincronizados (sem alteração): {already_synchronized_count}")
        print(f"  Novas atribuições a realizar: {new_assignments_count}")
        print(f"  Alterações de responsável a realizar: {responsible_changes_count}")
        print("-" * 70)
        print("DISTRIBUIÇÃO DOS CÓDOGOS ÚNICOS PROPOSTOS:")
        for r_name, count in report_metrics["proposed_unique_codes_by_responsible"].items():
            print(f"  {r_name}: {count}")
        print("=" * 70)

    finally:
        if db and hasattr(db, "close"):
            db.close()


if __name__ == "__main__":
    main()
