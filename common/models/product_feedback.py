from datetime import datetime
from sqlalchemy import BigInteger, Integer, String, DateTime, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin

class ProductFeedbackAttempt(TimestampMixin, Base):
    __tablename__ = 'xy_product_feedback_attempts'
    __table_args__ = (UniqueConstraint('owner_id','account_id','kind','order_no',name='uk_product_feedback_identity'),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer,'sqlite'),primary_key=True,autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    account_id: Mapped[str] = mapped_column(String(80))
    order_no: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    retry_at: Mapped[datetime | None] = mapped_column(DateTime,nullable=True)

class ProductRateTemplate(TimestampMixin, Base):
    __tablename__ = 'xy_product_rate_templates'
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer,'sqlite'),primary_key=True,autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger,index=True)
    account_id: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean,default=False)
