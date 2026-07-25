import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from repositories.contracts_control_repository import ContractsControlRepository
from models.contracts_control import (
    ContractsControlResponsible,
    ContractsControlManualData,
)

class ContractsControlManualService:
    @staticmethod
    def list_responsibles(db: Session, include_inactive: bool = False) -> List[ContractsControlResponsible]:
        return ContractsControlRepository.list_active_responsibles(db, include_inactive)

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
