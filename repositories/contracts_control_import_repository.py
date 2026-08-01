import uuid
from typing import List, Optional, Tuple
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from models.contracts_control import (
    ContractsControlImportPreview,
    ContractsControlImportPreviewItem
)

class ContractsControlImportRepository:
    @staticmethod
    def create_preview(db: Session, preview: ContractsControlImportPreview) -> ContractsControlImportPreview:
        db.add(preview)
        db.flush()
        return preview

    @staticmethod
    def create_preview_items(db: Session, items: List[ContractsControlImportPreviewItem]) -> None:
        db.add_all(items)
        db.flush()

    @staticmethod
    def get_preview_by_id(db: Session, preview_id: uuid.UUID) -> Optional[ContractsControlImportPreview]:
        return db.get(ContractsControlImportPreview, preview_id)

    @staticmethod
    def delete_expired_previews(db: Session, now_dt) -> int:
        if db.bind.dialect.name == "sqlite":
            if now_dt.tzinfo is not None:
                now_dt = now_dt.replace(tzinfo=None)
        stmt = select(ContractsControlImportPreview).where(ContractsControlImportPreview.expires_at <= now_dt)
        expired = db.scalars(stmt).all()
        count = len(expired)
        for exp in expired:
            db.delete(exp)
        db.flush()
        return count

    @staticmethod
    def get_preview_items_paginated(
        db: Session,
        preview_id: uuid.UUID,
        page: int,
        page_size: int,
        filters: dict
    ) -> Tuple[List[ContractsControlImportPreviewItem], int]:
        # 1. Build Query
        stmt = select(ContractsControlImportPreviewItem).where(ContractsControlImportPreviewItem.preview_id == preview_id)

        # Apply filters
        if filters.get("status"):
            stmt = stmt.where(ContractsControlImportPreviewItem.decisao_proposta == filters["status"])

        if filters.get("responsavel"):
            # Case insensitive check on responsavel_planilha
            resp_filter = str(filters["responsavel"]).strip()
            stmt = stmt.where(func.lower(ContractsControlImportPreviewItem.responsavel_planilha) == resp_filter.lower())

        if filters.get("codigo"):
            code_filter = str(filters["codigo"]).strip()
            stmt = stmt.where(ContractsControlImportPreviewItem.codigo_imovel == code_filter)

        if filters.get("aba"):
            stmt = stmt.where(ContractsControlImportPreviewItem.aba == filters["aba"])

        if filters.get("only_pending"):
            pending_statuses = ['ready_to_assign', 'ready_to_change', 'ready_to_clear']
            stmt = stmt.where(ContractsControlImportPreviewItem.decisao_proposta.in_(pending_statuses))

        # Order by sheet and line number
        stmt = stmt.order_by(ContractsControlImportPreviewItem.aba, ContractsControlImportPreviewItem.linha)

        # Get total count before pagination
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_count = db.scalar(count_stmt) or 0

        # Apply Pagination
        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        items = list(db.scalars(stmt).all())
        return items, total_count
