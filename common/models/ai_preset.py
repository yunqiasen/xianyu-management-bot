"""用户私有 AI 预设，应用时复制设置，不关联账号配置生命周期。"""
from __future__ import annotations

from typing import Any
from sqlalchemy import BigInteger, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin


class AIPreset(TimestampMixin, Base):
    __tablename__ = 'xy_ai_presets'

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
