"""Owner-scoped title rules; card SKU equality remains an independent requirement."""
from uuid import uuid4
from sqlalchemy import BigInteger, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin


class DeliveryRule(TimestampMixin, Base):
    __tablename__ = 'xy_delivery_rules'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_id: Mapped[str | None] = mapped_column(String(80), index=True)
    card_id: Mapped[int] = mapped_column(BigInteger, index=True)
    keyword: Mapped[str] = mapped_column(String(255))
    match_mode: Mapped[str] = mapped_column(String(24), default='contains')
    delivery_count: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(Text)
    delivery_times: Mapped[int] = mapped_column(Integer, default=0)
    legacy_id: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, default=1)
