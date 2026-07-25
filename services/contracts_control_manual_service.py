import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from repositories.contracts_control_repository import ContractsControlRepository
from models.contracts_control import (
    ContractsControlResponsible,
    ContractsControlManualData,
    normalize_responsible_name,
)

class ContractsControlManualService:
    @staticmethod
    def list_responsibles(db: Session, include_inactive: bool = False) -> List[ContractsControlResponsible]:
        return ContractsControlRepository.list_active_responsibles(db, include_inactive)

    @staticmethod
    def create_responsible(db: Session, name: str) -> ContractsControlResponsible:
        if not name or not name.strip():
            raise ValueError("empty_name")
        norm_name = normalize_responsible_name(name)
        existing = ContractsControlRepository.get_responsible_by_normalized_name(db, norm_name)
        if existing:
            raise ValueError("duplicate_name")
        return ContractsControlRepository.create_responsible(db, name)

    @staticmethod
    def update_responsible(
        db: Session,
        resp_id: uuid.UUID,
        name: Optional[str] = None,
        active: Optional[bool] = None
    ) -> ContractsControlResponsible:
        resp = ContractsControlRepository.get_responsible_by_id(db, resp_id)
        if not resp:
            raise ValueError("responsible_not_found")
        if name is not None:
            if not name or not name.strip():
                raise ValueError("empty_name")
            norm_name = normalize_responsible_name(name)
            existing = ContractsControlRepository.get_responsible_by_normalized_name(db, norm_name)
            if existing and existing.id != resp_id:
                raise ValueError("duplicate_name")
        updated = ContractsControlRepository.update_responsible(db, resp_id, name, active)
        if not updated:
            raise ValueError("responsible_not_found")
        return updated

    @staticmethod
    def update_individual_attribution(
        db: Session,
        transaction_id: str,
        responsible_id: Optional[uuid.UUID],
        expected_version: int,
        actor_sub: str
    ) -> tuple: # returns Tuple[ContractsControlManualData, bool]
        # Validate responsible if provided
        if responsible_id is not None:
            resp = ContractsControlRepository.get_responsible_by_id(db, responsible_id)
            if not resp:
                raise ValueError("responsible_not_found")
            if not resp.active:
                raise ValueError("responsible_inactive")

        md = ContractsControlRepository.get_manual_data_by_transaction_id(db, transaction_id)
        if not md:
            # Initial creation expects version 0
            if expected_version != 0:
                raise ValueError("version_conflict")

            # A) Sem registro, responsible_id null, version 0:
            # responsible = null, version = 0, changed = false. Não criar manual_data. Não criar histórico.
            if responsible_id is None:
                transient_md = ContractsControlManualData(
                    transaction_id=transaction_id,
                    responsible_id=None,
                    version=0,
                    updated_at=datetime.now(timezone.utc),
                    created_by_sub=actor_sub,
                    updated_by_sub=actor_sub
                )
                return transient_md, False

            # C) Alteração efetiva (criação):
            from sqlalchemy.exc import IntegrityError
            try:
                md = ContractsControlRepository.create_manual_data(
                    db, transaction_id, responsible_id, actor_sub
                )
            except IntegrityError:
                db.rollback()
                raise ValueError("version_conflict")

            prev_val = None
            new_val = str(responsible_id)
            ContractsControlRepository.create_history_record(
                db, transaction_id, "responsible_id", prev_val, new_val, 0, 1, actor_sub
            )
            return md, True
        else:
            # Subsequent updates expect exact version
            if md.version != expected_version:
                raise ValueError("version_conflict")

            # B) Mesmo responsável já atribuído:
            # version permanece igual, changed = false. Não criar histórico.
            if md.responsible_id == responsible_id:
                return md, False

            # C) Alteração efetiva:
            old_resp_id = md.responsible_id
            old_version = md.version
            new_version = old_version + 1

            success = ContractsControlRepository.update_manual_data_optimistic(
                db, transaction_id, responsible_id, old_version, actor_sub
            )
            if not success:
                raise ValueError("version_conflict")

            db.refresh(md)
            prev_val = str(old_resp_id) if old_resp_id else None
            new_val = str(responsible_id) if responsible_id else None
            ContractsControlRepository.create_history_record(
                db, transaction_id, "responsible_id", prev_val, new_val, old_version, new_version, actor_sub
            )
            return md, True

    @staticmethod
    def update_bulk_attribution(
        db: Session,
        items: List[Dict[str, Any]],
        responsible_id: Optional[uuid.UUID],
        actor_sub: str,
        valid_transaction_ids: set
    ) -> Dict[str, Any]:
        if not items:
            raise ValueError("items_empty")
        if len(items) > 100:
            raise ValueError("items_limit_exceeded")

        tx_ids = [item.get("transaction_id") for item in items]
        if any(not tid for tid in tx_ids):
            raise ValueError("empty_transaction_id")
        if len(tx_ids) != len(set(tx_ids)):
            raise ValueError("duplicate_transaction_ids")

        # Validate responsible if provided
        if responsible_id is not None:
            resp = ContractsControlRepository.get_responsible_by_id(db, responsible_id)
            if not resp:
                raise ValueError("responsible_not_found")
            if not resp.active:
                raise ValueError("responsible_inactive")

        # Check transaction IDs existence in Pipeimob dataset
        for tx_id in tx_ids:
            if tx_id not in valid_transaction_ids:
                raise ValueError(f"transaction_not_found:{tx_id}")

        requested_count = len(items)
        updated_count = 0
        unchanged_count = 0
        items_res = []

        nested = db.begin_nested()
        try:
            for item in items:
                tx_id = item["transaction_id"]
                expected_ver = item["version"]
                md, changed = ContractsControlManualService.update_individual_attribution(
                    db, tx_id, responsible_id, expected_ver, actor_sub
                )
                if changed:
                    updated_count += 1
                else:
                    unchanged_count += 1
                items_res.append({
                    "transaction_id": tx_id,
                    "version": md.version,
                    "changed": changed
                })
            nested.commit()
            db.commit()
        except Exception as e:
            try:
                nested.rollback()
            except Exception:
                pass
            db.rollback()
            raise e

        return {
            "requested_count": requested_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "items": items_res
        }

    @staticmethod
    def get_manual_data_for_overlay(db: Session, tx_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not tx_ids:
            return {}
        md_list = ContractsControlRepository.get_manual_data_by_transaction_ids(db, tx_ids)
        result = {}
        for md in md_list:
            resp_info = None
            if md.responsible:
                resp_info = {
                    "id": str(md.responsible.id),
                    "name": md.responsible.name,
                    "active": md.responsible.active
                }
            result[md.transaction_id] = {
                "responsible": resp_info,
                "version": md.version,
                "updated_at": md.updated_at,
                "updated_by_sub": md.updated_by_sub
            }
        return result

    @staticmethod
    def get_enrichment_indicators(db: Session, eligible_tx_ids: List[str]) -> Dict[str, Any]:
        eligible_count = len(eligible_tx_ids)
        if eligible_count == 0:
            return {
                "eligible_records_count": 0,
                "responsible_filled_count": 0,
                "responsible_pending_count": 0,
                "responsible_completion_ratio": 0.0,
                "last_manual_update_at": None
            }

        md_list = ContractsControlRepository.get_manual_data_by_transaction_ids(db, eligible_tx_ids)
        filled_count = len([md for md in md_list if md.responsible_id is not None])
        pending_count = eligible_count - filled_count

        last_update = None
        if md_list:
            last_update = max(md.updated_at for md in md_list)

        return {
            "eligible_records_count": eligible_count,
            "responsible_filled_count": filled_count,
            "responsible_pending_count": pending_count,
            "responsible_completion_ratio": float(filled_count / eligible_count) if eligible_count > 0 else 0.0,
            "last_manual_update_at": last_update
        }
