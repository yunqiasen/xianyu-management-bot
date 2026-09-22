"""订单历史按页检查点（与导入同一事务提交）。"""
from __future__ import annotations
from uuid import uuid4
from sqlalchemy import BigInteger,Integer,String
from sqlalchemy.orm import Mapped,mapped_column
from common.db.base_class import Base,TimestampMixin

class OrderSyncJob(TimestampMixin,Base):
    __tablename__='xy_order_sync_jobs'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=lambda:str(uuid4()))
    owner_id: Mapped[int]=mapped_column(BigInteger,index=True)
    account_id: Mapped[str]=mapped_column(String(64),index=True)
    status: Mapped[str]=mapped_column(String(32),default='pending')
    next_page: Mapped[int]=mapped_column(Integer,default=1)
    total_pages: Mapped[int]=mapped_column(Integer,default=0)
    imported: Mapped[int]=mapped_column(Integer,default=0)
    revision: Mapped[int]=mapped_column(Integer,default=0)
    lease_until: Mapped[int]=mapped_column(BigInteger,default=0)
    last_error: Mapped[str | None]=mapped_column(String(120))
