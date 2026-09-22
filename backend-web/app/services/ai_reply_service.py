"""
AI回复设置服务

功能：
1. 管理账号的AI回复配置
2. 存储在账号metadata JSON中
3. 支持模型名称、API密钥、折扣设置等
"""
from __future__ import annotations

from copy import deepcopy
import json

from sqlalchemy import select, update
from common.schemas.ai_reply import AIReplySettings
from common.services.ai_gateway import AIConfig, SECRET_MASK, is_secret_placeholder
from sqlalchemy.ext.asyncio import AsyncSession

from common.services.ai_provider_service import (
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_PROVIDER_TYPE,
    clean_ai_text,
    get_ai_settings_missing_fields,
    normalize_ai_provider_type,
    read_ai_enabled,
)
from common.models.xy_account import XYAccount

DEFAULT_AI_SETTINGS = AIReplySettings().model_dump(exclude={"api_key_configured"})


def public_ai_settings(settings: dict) -> dict:
    payload = deepcopy(settings)
    payload["api_key_configured"] = bool(payload.get("api_key"))
    payload["api_key"] = SECRET_MASK if payload["api_key_configured"] else ""
    return payload


def merge_ai_settings(existing: dict, payload: dict) -> dict:
    """账号和预设共用相同的保存校验；空白/占位密钥表示保留。"""
    merged = deepcopy(existing)
    for key, value in payload.items():
        if key in DEFAULT_AI_SETTINGS and key not in {"api_key", "config_version"} and value is not None:
            merged[key] = value
    if "enabled" in payload and "ai_enabled" not in payload:
        merged["ai_enabled"] = bool(payload["enabled"])
    key = payload.get("api_key")
    if key and str(key).strip() and not is_secret_placeholder(key):
        merged["api_key"] = str(key).strip()
    if payload.get("clear_api_key"):
        merged["api_key"] = ""
    merged["provider_type"] = normalize_ai_provider_type(merged.get("provider_type"), merged.get("base_url"), merged.get("model_name"))
    merged["manual_reply_ai_pause_minutes"] = max(1, min(1440, int(merged.get("manual_reply_ai_pause_minutes") or 10)))
    for key in ("max_discount_percent", "max_discount_amount", "max_bargain_rounds"):
        merged[key] = int(merged.get(key) or 0)
        if merged[key] < 0:
            raise ValueError("议价配置应为非负数")
    prompts = merged.get("custom_prompts") or ""
    if isinstance(prompts, str) and prompts.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(prompts)
        except ValueError as exc:
            raise ValueError("提示词 JSON 格式错误") from exc
        if not isinstance(parsed, dict) or any(key not in {"default", "price", "tech"} or not isinstance(value, str) for key, value in parsed.items()):
            raise ValueError("提示词 JSON 仅支持 default / price / tech 文本字段")
    validation = dict(merged)
    if not merged.get("ai_enabled") and not validation.get("api_key"):
        validation["api_key"] = "disabled-validation-only"
    AIConfig.from_settings(validation)
    merged["enabled"] = bool(merged.get("ai_enabled"))
    return merged


class AIReplySettingsService:
    """Stores AI reply settings within the XYAccount metadata JSON blob."""

    def __init__(self, session: AsyncSession):
        self.session = session

    def _extract_settings(self, account: XYAccount) -> dict:
        stored = (account.metadata_json or {}).get("ai_reply_settings") or {}
        payload = DEFAULT_AI_SETTINGS.copy()
        payload.update({k: v for k, v in stored.items() if v is not None})
        payload["ai_enabled"] = read_ai_enabled(stored)
        payload["max_discount_percent"] = int(payload.get("max_discount_percent", 10) or 0)
        payload["max_discount_amount"] = int(payload.get("max_discount_amount", 100) or 0)
        payload["max_bargain_rounds"] = int(payload.get("max_bargain_rounds", 3) or 0)
        payload["provider_type"] = normalize_ai_provider_type(
            payload.get("provider_type"),
            payload.get("base_url"),
            payload.get("model_name"),
        )
        payload["model_name"] = clean_ai_text(payload.get("model_name"))
        payload["api_key"] = clean_ai_text(payload.get("api_key"))
        payload["base_url"] = clean_ai_text(payload.get("base_url"))
        payload["custom_prompts"] = payload.get("custom_prompts") or ""
        payload["ai_time_range_start"] = payload.get("ai_time_range_start") or ""
        payload["ai_time_range_end"] = payload.get("ai_time_range_end") or ""
        payload["manual_reply_ai_pause_enabled"] = bool(
            payload.get("manual_reply_ai_pause_enabled", False)
        )
        payload["manual_reply_ai_pause_minutes"] = max(
            1, min(1440, int(payload.get("manual_reply_ai_pause_minutes", 10) or 10))
        )
        return payload

    async def get_settings(self, account: XYAccount) -> dict:
        return self._extract_settings(account)

    async def update_settings(self, account: XYAccount, payload: dict) -> dict:
        # 持锁重新读取，避免用旧 JSON 覆盖其他领域刚保存的账号配置。
        result = await self.session.execute(
            select(XYAccount).where(XYAccount.id == account.id, XYAccount.owner_id == account.owner_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        current = result.scalar_one_or_none()
        if current is None:
            raise ValueError("账号不存在")
        existing = self._extract_settings(current)
        expected = payload.get("config_version")
        if expected is not None and int(expected) != int(existing.get("config_version") or 0):
            raise ValueError("AI 配置已更新，请重新加载后保存")
        merged = merge_ai_settings(existing, payload)
        merged["config_version"] = int(existing.get("config_version") or 0) + 1
        metadata = deepcopy(current.metadata_json or {})
        metadata["ai_reply_settings"] = merged
        await self.session.execute(update(XYAccount).where(XYAccount.id == current.id).values(metadata_json=metadata))
        await self.session.commit()
        account.metadata_json = metadata
        return merged

    async def list_settings(self, owner_id: int) -> dict[str, dict]:
        stmt = select(XYAccount).where(XYAccount.owner_id == owner_id)
        result = await self.session.execute(stmt)
        accounts = result.scalars().all()
        return {account.account_id: self._extract_settings(account) for account in accounts}
