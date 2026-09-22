"""账号 RPC 持久去重账本；不保存 Cookie、令牌或请求正文。"""
from sqlalchemy import BigInteger, Double, Float, JSON, String, inspect
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin


class AccountOperation(TimestampMixin, Base):
    __tablename__ = 'xy_account_operations'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    command: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(BigInteger)
    credential_version: Mapped[int] = mapped_column(BigInteger)
    config_version: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), index=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    deadline: Mapped[float] = mapped_column(Double)


def upgrade_account_operations(connection):
    """受控迁移入口；MySQL 单精度 FLOAT 会截断 epoch 秒，截止时间必须双精度。"""
    AccountOperation.__table__.create(connection, checkfirst=True)
    if connection.dialect.name in {'mysql', 'mariadb'}:
        columns = {c['name']: c for c in inspect(connection).get_columns(AccountOperation.__tablename__)}
        if columns['request_id']['type'].length < 128:
            connection.exec_driver_sql('ALTER TABLE xy_account_operations MODIFY COLUMN request_id VARCHAR(128) NOT NULL')
        column = columns['deadline']
        if column['type'].__class__.__name__.upper() == 'FLOAT':
            connection.exec_driver_sql('ALTER TABLE xy_account_operations MODIFY COLUMN deadline DOUBLE NOT NULL')
