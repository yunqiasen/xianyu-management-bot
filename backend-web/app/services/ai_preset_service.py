"""AI 预设 CRUD 与逐账号应用。所有读写均有 owner_id 条件。"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from common.models.ai_preset import AIPreset
from common.models.xy_account import XYAccount
from app.services.ai_reply_service import AIReplySettingsService, DEFAULT_AI_SETTINGS, merge_ai_settings, public_ai_settings


class AIPresetService:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _public(row: AIPreset) -> dict:
        return {'id': row.id, 'name': row.name, 'settings': public_ai_settings(row.settings_json)}

    async def _get(self, owner_id: int, preset_id: str, *, lock: bool = False) -> AIPreset:
        stmt = select(AIPreset).where(AIPreset.id == preset_id, AIPreset.owner_id == owner_id)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ValueError('预设不存在')
        return row

    async def list(self, owner_id: int) -> list[dict]:
        rows = (await self.session.execute(select(AIPreset).where(AIPreset.owner_id == owner_id).order_by(AIPreset.created_at, AIPreset.id))).scalars().all()
        return [self._public(row) for row in rows]

    async def create(self, owner_id: int, name: str, settings: dict, *, source_account_id: str | None = None) -> dict:
        if not name.strip() or len(name.strip()) > 120:
            raise ValueError('预设名称需为 1–120 字符')
        existing = deepcopy(DEFAULT_AI_SETTINGS)
        if source_account_id:
            account = (await self.session.execute(select(XYAccount).where(XYAccount.owner_id == owner_id, XYAccount.account_id == source_account_id))).scalar_one_or_none()
            if account is None:
                raise ValueError('来源账号不存在')
            existing = await AIReplySettingsService(self.session).get_settings(account)
        snapshot = merge_ai_settings(existing, settings)
        snapshot.pop('config_version', None)
        row = AIPreset(id=uuid4().hex, owner_id=owner_id, name=name.strip(), settings_json=snapshot)
        self.session.add(row)
        await self.session.commit()
        return self._public(row)

    async def update(self, owner_id: int, preset_id: str, *, name: str | None = None, settings: dict | None = None) -> dict:
        row = await self._get(owner_id, preset_id, lock=True)
        if name is not None:
            if not name.strip() or len(name.strip()) > 120:
                raise ValueError('预设名称需为 1–120 字符')
            row.name = name.strip()
        if settings is not None:
            snapshot = merge_ai_settings(row.settings_json, settings)
            snapshot.pop('config_version', None)
            row.settings_json = snapshot
        await self.session.commit()
        return self._public(row)

    async def delete(self, owner_id: int, preset_id: str) -> None:
        row = await self._get(owner_id, preset_id, lock=True)
        await self.session.delete(row)
        await self.session.commit()

    async def apply(self, owner_id: int, preset_id: str, account_ids: list[str]) -> list[dict]:
        preset = await self._get(owner_id, preset_id)
        # 在首个账号提交之前复制，避免删除/修改预设影响同一次批量操作。
        snapshot = deepcopy(preset.settings_json)
        snapshot.pop('config_version', None)
        snapshot['clear_api_key'] = not bool(snapshot.get('api_key'))
        results = []
        for account_id in dict.fromkeys(account_ids):
            try:
                account = (await self.session.execute(select(XYAccount).where(XYAccount.owner_id == owner_id, XYAccount.account_id == account_id))).scalar_one_or_none()
                if account is None:
                    raise ValueError('账号不存在')
                saved = await AIReplySettingsService(self.session).update_settings(account, deepcopy(snapshot))
                results.append({'account_id': account_id, 'success': True, 'message': '已复制预设', 'config_version': saved['config_version']})
            except Exception as exc:
                await self.session.rollback()
                message = str(exc) if isinstance(exc, ValueError) else '账号配置写入失败'
                results.append({'account_id': account_id, 'success': False, 'message': message})
        return results
