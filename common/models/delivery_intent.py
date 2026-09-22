"""持久履约意图：预占、内容发送、平台确认各自留痕。"""
from __future__ import annotations
from uuid import uuid4
from sqlalchemy import BigInteger, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin

class DeliveryIntent(TimestampMixin, Base):
    __tablename__ = 'xy_delivery_intents'
    __table_args__ = (UniqueConstraint('owner_id','account_id','order_no','operation_key',name='uq_delivery_operation'),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True,default=lambda:str(uuid4()))
    owner_id: Mapped[int] = mapped_column(BigInteger,index=True)
    account_id: Mapped[str] = mapped_column(String(64),index=True)
    order_no: Mapped[str] = mapped_column(String(64),index=True)
    line_id: Mapped[str] = mapped_column(String(80), default='main', server_default='main')
    operation_key: Mapped[str] = mapped_column(String(64),default='payment')
    card_id: Mapped[int] = mapped_column(BigInteger)
    card_type: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(32),default='send_first')
    content_state: Mapped[str] = mapped_column(String(32),default='reserved')
    confirm_state: Mapped[str] = mapped_column(String(32),default='pending')
    payload: Mapped[dict | None] = mapped_column(JSON)
    reserved_lines: Mapped[list | None] = mapped_column(JSON)
    source_snapshot: Mapped[dict | None] = mapped_column(JSON)
    generation: Mapped[int] = mapped_column(Integer,default=0)
    credential_version: Mapped[int] = mapped_column(Integer,default=0)
    config_version: Mapped[int] = mapped_column(Integer,default=0)
    confirm_attempts: Mapped[int] = mapped_column(Integer,default=0)
    next_retry_at: Mapped[int] = mapped_column(BigInteger,default=0)
    not_before: Mapped[int] = mapped_column(BigInteger,default=0)
    last_error: Mapped[str | None] = mapped_column(String(120))
    evidence: Mapped[list | None] = mapped_column(JSON)
