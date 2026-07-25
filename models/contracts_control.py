import uuid
import unicodedata
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Boolean, DateTime, ForeignKey, Integer, UniqueConstraint, Index, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base

def normalize_responsible_name(name: str) -> str:
    if not name:
        return ""
    # spaces strip
    name = name.strip()
    # collapse duplicate spaces
    name = " ".join(name.split())
    # lowercase
    name = name.lower()
    # remove accents
    name = "".join(
        c for c in unicodedata.normalize("NFKD", name)
        if not unicodedata.combining(c)
    )
    return name

class ContractsControlResponsible(Base):
    __tablename__ = "contracts_control_responsibles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_normalized_name"),
        CheckConstraint("length(btrim(name)) > 0", name="chk_responsible_name_not_empty"),
        CheckConstraint("length(btrim(normalized_name)) > 0", name="chk_responsible_normalized_name_not_empty"),
    )

class ContractsControlManualData(Base):
    __tablename__ = "contracts_control_manual_data"

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True, nullable=False)
    responsible_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("contracts_control_responsibles.id"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    created_by_sub: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    updated_by_sub: Mapped[str] = mapped_column(String, nullable=False)

    responsible = relationship("ContractsControlResponsible")

    __table_args__ = (
        Index("idx_manual_data_responsible_id", "responsible_id"),
        Index("idx_manual_data_updated_at", "updated_at"),
        CheckConstraint("version >= 1", name="chk_manual_data_version_min"),
    )

class ContractsControlManualDataHistory(Base):
    __tablename__ = "contracts_control_manual_data_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[str] = mapped_column(String, nullable=False)
    field_name: Mapped[str] = mapped_column(String, nullable=False)
    previous_value: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    previous_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    new_version: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    changed_by_sub: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_history_transaction_id", "transaction_id"),
        Index("idx_history_changed_at", "changed_at"),
    )
