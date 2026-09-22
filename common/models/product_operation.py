"""Durable product batches and append-only operator evidence (no credentials)."""
from datetime import datetime
from sqlalchemy import BigInteger, Integer, String, JSON, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin

class ProductPublishBatch(TimestampMixin, Base):
    __tablename__ = 'xy_product_publish_batches'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_ids: Mapped[list] = mapped_column(JSON)
    material_count: Mapped[int] = mapped_column(Integer)
    cancelled: Mapped[bool] = mapped_column(default=False)

class ProductOperationEvidence(TimestampMixin, Base):
    __tablename__ = 'xy_product_operation_evidence'
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer,'sqlite'), primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    target: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(40))
    detail: Mapped[dict] = mapped_column(JSON)
