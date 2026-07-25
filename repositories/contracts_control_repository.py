import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session
from models.contracts_control import (
    ContractsControlResponsible,
    ContractsControlManualData,
    ContractsControlManualDataHistory,
    normalize_responsible_name,
)

class ContractsControlRepository:
    @staticmethod
    def list_active_responsibles(db: Session, include_inactive: bool = False) -> List[ContractsControlResponsible]:
        stmt = select(ContractsControlResponsible)
        if not include_inactive:
            stmt = stmt.where(ContractsControlResponsible.active == True)
        stmt = stmt.order_by(ContractsControlResponsible.name)
        return list(db.scalars(stmt).all())

    @staticmethod
    def get_responsible_by_id(db: Session, resp_id: uuid.UUID) -> Optional[ContractsControlResponsible]:
        return db.get(ContractsControlResponsible, resp_id)

    @staticmethod
    def get_responsible_by_normalized_name(db: Session, name: str) -> Optional[ContractsControlResponsible]:
        norm_name = normalize_responsible_name(name)
        stmt = select(ContractsControlResponsible).where(ContractsControlResponsible.normalized_name == norm_name)
        return db.scalars(stmt).first()

    @staticmethod
    def get_manual_data_by_transaction_id(db: Session, tx_id: str) -> Optional[ContractsControlManualData]:
        return db.get(ContractsControlManualData, tx_id)

    @staticmethod
    def get_manual_data_by_transaction_ids(db: Session, tx_ids: List[str]) -> List[ContractsControlManualData]:
        if not tx_ids:
            return []
        stmt = select(ContractsControlManualData).where(ContractsControlManualData.transaction_id.in_(tx_ids))
        return list(db.scalars(stmt).all())

    @staticmethod
    def create_responsible(db: Session, name: str, active: bool = True) -> ContractsControlResponsible:
        norm_name = normalize_responsible_name(name)
        resp = ContractsControlResponsible(
            id=uuid.uuid4(),
            name=name,
            normalized_name=norm_name,
            active=active
        )
        db.add(resp)
        db.flush()
        return resp

    @staticmethod
    def create_manual_data(
        db: Session,
        tx_id: str,
        responsible_id: Optional[uuid.UUID],
        created_by_sub: str
    ) -> ContractsControlManualData:
        md = ContractsControlManualData(
            transaction_id=tx_id,
            responsible_id=responsible_id,
            version=1,
            created_by_sub=created_by_sub,
            updated_by_sub=created_by_sub
        )
        db.add(md)
        db.flush()
        return md

    @staticmethod
    def update_manual_data_optimistic(
        db: Session,
        tx_id: str,
        responsible_id: Optional[uuid.UUID],
        expected_version: int,
        actor_sub: str
    ) -> bool:
        stmt = (
            update(ContractsControlManualData)
            .where(
                ContractsControlManualData.transaction_id == tx_id,
                ContractsControlManualData.version == expected_version
            )
            .values(
                responsible_id=responsible_id,
                version=ContractsControlManualData.version + 1,
                updated_by_sub=actor_sub,
                updated_at=func.now()
            )
        )
        res = db.execute(stmt)
        return res.rowcount > 0

    @staticmethod
    def create_history_record(
        db: Session,
        transaction_id: str,
        field_name: str,
        previous_value: Optional[str],
        new_value: Optional[str],
        previous_version: Optional[int],
        new_version: int,
        changed_by_sub: str
    ) -> ContractsControlManualDataHistory:
        hist = ContractsControlManualDataHistory(
            id=uuid.uuid4(),
            transaction_id=transaction_id,
            field_name=field_name,
            previous_value=previous_value,
            new_value=new_value,
            previous_version=previous_version,
            new_version=new_version,
            changed_by_sub=changed_by_sub
        )
        db.add(hist)
        db.flush()
        return hist

    @staticmethod
    def get_history_by_transaction_id(db: Session, tx_id: str) -> List[ContractsControlManualDataHistory]:
        stmt = (
            select(ContractsControlManualDataHistory)
            .where(ContractsControlManualDataHistory.transaction_id == tx_id)
            .order_by(ContractsControlManualDataHistory.changed_at.asc())
        )
        return list(db.scalars(stmt).all())
