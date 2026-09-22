"""DEV46 sidecar tables. Additive migration; never rewrite existing task switches."""
from sqlalchemy import BigInteger, Boolean, Double, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base


class ListingMonitorState(Base):
    __tablename__ = 'xy_listing_monitor_state'
    task_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    region: Mapped[str] = mapped_column(String(120), default='')
    query_key: Mapped[str] = mapped_column(String(64), default='')
    generation: Mapped[int] = mapped_column(Integer, default=0)
    expected_pages: Mapped[int] = mapped_column(Integer, default=1)
    baseline_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    phase: Mapped[str] = mapped_column(String(32), default='pending')
    lease_until: Mapped[float] = mapped_column(Double, default=0)
    lease_token: Mapped[str] = mapped_column(String(36), default='')


class ListingMonitorPage(Base):
    __tablename__ = 'xy_listing_monitor_pages'
    __table_args__ = (UniqueConstraint('task_id', 'generation', 'page'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger, index=True)
    generation: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[str] = mapped_column(String(80))
    keyword: Mapped[str] = mapped_column(String(200))
    query_key: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(96))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default='pending')
    items: Mapped[list] = mapped_column(JSON, default=list)


class ListingMonitorObservation(Base):
    __tablename__ = 'xy_listing_monitor_observations'
    __table_args__ = (UniqueConstraint('task_id', 'query_key', 'item_id'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger, index=True)
    query_key: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(String(64))
    price: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON)


class ListingMonitorEvent(Base):
    __tablename__ = 'xy_listing_monitor_events'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_id: Mapped[str] = mapped_column(String(80))
    item_id: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    old_price: Mapped[str | None] = mapped_column(String(32))
    price: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(String(1000))
    notification_event_id: Mapped[str | None] = mapped_column(String(64), index=True)
    enqueue_status: Mapped[str] = mapped_column(String(32), default='pending')
