from sqlalchemy import BigInteger, Integer, String, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin

class ProductPolishRun(TimestampMixin, Base):
    __tablename__='xy_product_polish_runs'
    __table_args__=(UniqueConstraint('owner_id','account_id','cycle',name='uk_product_polish_run_cycle'),)
    id: Mapped[int]=mapped_column(BigInteger().with_variant(Integer,'sqlite'),primary_key=True,autoincrement=True)
    owner_id: Mapped[int]=mapped_column(BigInteger,index=True)
    account_id: Mapped[str]=mapped_column(String(80))
    cycle: Mapped[str | None]=mapped_column(String(100),nullable=True)
    source: Mapped[str]=mapped_column(String(20))
    status: Mapped[str]=mapped_column(String(40))
    result: Mapped[dict | None]=mapped_column(JSON,nullable=True)
