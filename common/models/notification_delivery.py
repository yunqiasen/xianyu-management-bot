"""通知事实、渲染快照与每渠道投递分别保存。"""
from __future__ import annotations
from uuid import uuid4
from sqlalchemy import String, Integer, JSON, Double, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin

class NotificationEvent(TimestampMixin, Base):
    __tablename__='xy_notify_events'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    owner_id: Mapped[int]=mapped_column(Integer,index=True)
    account_id: Mapped[str]=mapped_column(String(80),index=True)
    event_type: Mapped[str]=mapped_column(String(40))
    generation: Mapped[str]=mapped_column(String(128))
    payload: Mapped[dict]=mapped_column(JSON)
    merged_count: Mapped[int]=mapped_column(Integer,default=1)
    first_seen: Mapped[float]=mapped_column(Double)
    last_seen: Mapped[float]=mapped_column(Double)

class NotificationDelivery(TimestampMixin, Base):
    __tablename__='xy_notify_deliveries'
    __table_args__=(UniqueConstraint('event_id','channel_id'),)
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4()))
    event_id: Mapped[str]=mapped_column(String(64),index=True)
    channel_id: Mapped[int]=mapped_column(Integer)
    owner_id: Mapped[int]=mapped_column(Integer,index=True)
    rendered: Mapped[str]=mapped_column(Text)
    template_failed: Mapped[bool]=mapped_column(default=False)
    status: Mapped[str]=mapped_column(String(32),default='pending',index=True)
    attempts: Mapped[int]=mapped_column(Integer,default=0)
    due_at: Mapped[float]=mapped_column(Double)

class NotificationTemplate(TimestampMixin, Base):
    __tablename__='xy_notify_templates'
    __table_args__=(UniqueConstraint('owner_id','event_type'),)
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4()))
    owner_id: Mapped[int]=mapped_column(Integer,index=True)
    event_type: Mapped[str]=mapped_column(String(40))
    body: Mapped[str]=mapped_column(Text)
