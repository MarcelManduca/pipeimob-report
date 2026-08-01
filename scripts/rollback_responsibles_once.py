#!/usr/bin/env python3
"""
scripts/rollback_responsibles_once.py

Rotina CLI de Rollback Seguro para a Carga Única de Responsáveis.
Garante:
- Versão monotônica (version cresce a cada rollback; não decrementa).
- Detecção e bloqueio de conflitos em caso de alteração posterior por usuário (rollback_conflict).
- Gravação de auditoria em contracts_control_manual_data_history com changed_by_sub = "rollback:<execution_id>".
- Transação atômica única com rollback integral em caso de falha.
"""

import sys
import os
import argparse
import json
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

# Add parent directory to sys.path if running as script inside scripts/
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database
from repositories.contracts_control_repository import ContractsControlRepository
from models.contracts_control import ContractsControlManualData

ALLOWED_APP_ENVS = {"staging", "production", "development", "test"}


def main():
    parser = argparse.ArgumentParser(description="Rotina CLI de Rollback Seguro da Carga Única")
    parser.add_argument("--execution-id", required=True, help="Execution ID da carga a ser revertida")
    parser.add_argument("--target", required=True, choices=["staging", "production"], help="Ambiente alvo (staging ou production)")
    parser.add_argument("--apply", action="store_true", help="Executa a escrita de rollback no banco. Sem esta flag, apenas simula.")
    parser.add_argument("--confirm-production", action="store_true", help="Confirmação necessária para rollback em produção")

    args = parser.parse_args()

    # 1. Validar APP_ENV do sistema
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if not app_env or app_env not in ALLOWED_APP_ENVS:
        print(f"ABORTADO: APP_ENV ausente, vazio ou desconhecido: '{app_env}'. Esperado: staging, production, development ou test.", file=sys.stderr)
        sys.exit(1)

    # 2. Validar coincidência de target com APP_ENV
    if args.target == "staging" and app_env not in ("staging", "development", "test"):
        print(f"ABORTADO: --target staging especificado, mas APP_ENV real é '{app_env}'.", file=sys.stderr)
        sys.exit(1)

    if args.target == "production" and app_env != "production":
        print(f"ABORTADO: --target production especificado, mas APP_ENV real é '{app_env}'.", file=sys.stderr)
        sys.exit(1)

    if args.apply and args.target == "production" and not args.confirm_production:
        print("ABORTADO: Rollback com --apply em production exige a flag --confirm-production.", file=sys.stderr)
        sys.exit(1)

    execution_id = args.execution_id.strip()
    backup_file = os.path.join("backups", f"import_once_{execution_id}", "backup_pre_import.json")

    if not os.path.isfile(backup_file):
        print(f"ABORTADO: Arquivo de backup pré-importação '{backup_file}' não foi encontrado.", file=sys.stderr)
        sys.exit(1)

    with open(backup_file, "r", encoding="utf-8") as f:
        snapshot_backup: List[Dict[str, Any]] = json.load(f)

    if not database.SessionLocal:
        db_url = os.getenv("DATABASE_URL") or "sqlite:///test.db"
        database.init_db(db_url)

    if database.engine and "sqlite" in str(database.engine.url):
        from sqlalchemy import event
        @event.listens_for(database.engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            dbapi_connection.create_function("btrim", 1, lambda s: s.strip() if s is not None else None)
        from models.contracts_control import Base
        Base.metadata.create_all(bind=database.engine)

    db = database.SessionLocal()
    try:
        eligible_rollbacks: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []

        for item in snapshot_backup:
            tx_id = item["transaction_id"]
            applied_resp_id = item["applied_responsible_id"]
            applied_ver = item["applied_version"]
            pre_existing = item["pre_existing"]
            prev_resp_id = item["previous_responsible_id"]
            prev_ver = item["previous_version"]

            current_md = ContractsControlRepository.get_manual_data_by_transaction_id(db, tx_id)

            if not current_md:
                # O registro manual não existe no banco (pode ter sido apagado manualmente)
                conflicts.append({
                    "transaction_id": tx_id,
                    "codigo_imovel": item["codigo_imovel"],
                    "reason": "rollback_conflict_record_deleted",
                    "details": "Registro manual não encontrado no banco."
                })
                continue

            current_resp_id_str = str(current_md.responsible_id) if current_md.responsible_id else None

            # Checar se houve alteração posterior por usuário no banco
            if current_resp_id_str != applied_resp_id or current_md.version != applied_ver:
                conflicts.append({
                    "transaction_id": tx_id,
                    "codigo_imovel": item["codigo_imovel"],
                    "reason": "rollback_conflict",
                    "details": {
                        "expected_applied_resp_id": applied_resp_id,
                        "current_resp_id": current_resp_id_str,
                        "expected_applied_version": applied_ver,
                        "current_version": current_md.version
                    }
                })
                continue

            # Item elegível para rollback monotônico
            eligible_rollbacks.append({
                "transaction_id": tx_id,
                "codigo_imovel": item["codigo_imovel"],
                "pre_existing": pre_existing,
                "applied_responsible_id": applied_resp_id,
                "applied_version": applied_ver,
                "previous_responsible_id": uuid.UUID(prev_resp_id) if prev_resp_id else None,
                "previous_version": prev_ver,
                "current_version": current_md.version
            })

        db.close()

        print("=" * 70)
        print(f"RELATÓRIO DE SIMULAÇÃO DE ROLLBACK — EXECUTION ID: {execution_id}")
        print("=" * 70)
        print(f"Modo: {'APPLY (Escrita)' if args.apply else 'DRY-RUN (Simulação)'}")
        print(f"Target: {args.target} (APP_ENV: {app_env})")
        print(f"Total de itens no snapshot: {len(snapshot_backup)}")
        print(f"Elegíveis para rollback: {len(eligible_rollbacks)}")
        print(f"Conflitos de alteração posterior (rollback_conflict): {len(conflicts)}")
        print("=" * 70)

        if conflicts:
            print("ITENS EM CONFLITO (NÃO SERÃO ALTERADOS):")
            for c in conflicts:
                print(f"  Imóvel {c['codigo_imovel']} (tx_id: {c['transaction_id']}): {c['reason']}")

        # Execução de escrita com --apply
        rollback_applied_count = 0
        actor_sub = f"rollback:{execution_id}"

        if args.apply and eligible_rollbacks:
            write_db = database.SessionLocal()
            try:
                for rb in eligible_rollbacks:
                    tx_id = rb["transaction_id"]
                    applied_ver = rb["applied_version"]
                    restored_resp_id = rb["previous_responsible_id"]
                    applied_resp_id = rb["applied_responsible_id"]

                    # Otimistic update restaurando o responsável anterior e incrementando a versão monotonicamente (v2 -> v3)
                    success = ContractsControlRepository.update_manual_data_optimistic(
                        write_db, tx_id, restored_resp_id, applied_ver, actor_sub
                    )
                    if not success:
                        raise ValueError(f"Rollback optimistic lock conflict on transaction_id {tx_id}")

                    # Gravar histórico de auditoria do rollback
                    ContractsControlRepository.create_history_record(
                        write_db,
                        tx_id,
                        "responsible_id",
                        applied_resp_id,
                        str(restored_resp_id) if restored_resp_id else None,
                        applied_ver,
                        applied_ver + 1,
                        actor_sub
                    )

                    rollback_applied_count += 1

                write_db.commit()
                print(f"SUCESSO: Rollback transacional concluído. {rollback_applied_count} itens revertidos monotonicamente.")
            except Exception as e:
                write_db.rollback()
                print(f"ERRO CRÍTICO NO ROLLBACK: Rollback integral executado. Causa: {e}", file=sys.stderr)
                raise
            finally:
                write_db.close()

    finally:
        if db and hasattr(db, "close"):
            db.close()


if __name__ == "__main__":
    main()
