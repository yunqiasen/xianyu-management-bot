"""Account-scoped polishing window; existing auto_polish switch remains authoritative."""
from sqlalchemy import BigInteger, Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin

class ProductPolishSchedule(TimestampMixin, Base):
    __tablename__ = 'xy_product_polish_schedules'
    __table_args__ = (UniqueConstraint('owner_id', 'account_id', name='uk_product_polish_account'),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'), primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[str] = mapped_column(String(80), nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), default='Asia/Shanghai')
    start: Mapped[str] = mapped_column(String(5), default='09:00')
    end: Mapped[str] = mapped_column(String(5), default='10:00')
    last_cycle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    randomize: Mapped[bool] = mapped_column(Boolean, default=False)
    planned_cycle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    planned_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
