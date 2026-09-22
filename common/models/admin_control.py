"""管理防护、操作审计和隔离恢复证据；新增表由统一迁移入口注册。"""
from __future__ import annotations
from uuid import uuid4
from sqlalchemy import String, Integer, Double, JSON, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin

class LoginProtection(Base):
    __tablename__ = 'xy_admin_login_protection'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(16), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    failures: Mapped[list] = mapped_column(JSON, default=list)
    locked_until: Mapped[float] = mapped_column(Double, default=0)

class AdminAudit(TimestampMixin, Base):
    __tablename__ = 'xy_admin_audit'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(255))
    details: Mapped[dict] = mapped_column(JSON, default=dict)

class BackupVerification(TimestampMixin, Base):
    __tablename__ = 'xy_admin_backup_verification'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    backup_log_id: Mapped[int] = mapped_column(Integer, index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default='checked')
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    protected: Mapped[bool] = mapped_column(Boolean, default=False)

class DataPreview(TimestampMixin, Base):
    __tablename__ = 'xy_admin_data_preview'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    actor_id: Mapped[int] = mapped_column(Integer)
    table_name: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Double)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)

class AdminLogArchive(TimestampMixin, Base):
    __tablename__ = 'xy_admin_log_archive'
    __table_args__ = (UniqueConstraint('source_table', 'source_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    source_table: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)

class LoginProtectionConfig(TimestampMixin, Base):
    __tablename__ = 'xy_admin_login_protection_config'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    values: Mapped[dict] = mapped_column(JSON, default=dict)


class AdminAuditArchive(TimestampMixin, Base):
    """Original audit identity survives the move out of the online table."""
    __tablename__ = 'xy_admin_audit_archive'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
