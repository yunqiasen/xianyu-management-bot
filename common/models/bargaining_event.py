"""P5 候选：独立保留的买家议价事实；清理聊天展示历史不级联删除。"""
from sqlalchemy import Double, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from common.db.base_class import Base, TimestampMixin


from common.models.identity import identity_string


class BargainingEvent(TimestampMixin, Base):
    __tablename__ = 'xy_bargaining_events'
    __table_args__ = (
        UniqueConstraint('account_id', 'chat_id', 'event_id', name='uq_bargaining_event_identity'),
        Index('ix_bargaining_conversation', 'account_id', 'chat_id'),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(identity_string(80), nullable=False)
    chat_id: Mapped[str] = mapped_column(identity_string(128), nullable=False)
    event_id: Mapped[str] = mapped_column(identity_string(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    classifier_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurred_at: Mapped[float | None] = mapped_column(Double, nullable=True)
