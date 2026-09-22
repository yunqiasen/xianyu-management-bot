"""
认证服务

功能：
1. 用户名/邮箱密码认证
2. 密码验证（支持SHA256和passlib格式）
3. 登录时间记录
4. 访问令牌生成
5. 用户名/IP独立15分钟滑窗防护
"""
from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from typing import Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from common.models.user import User
from common.utils.time_utils import BEIJING_TZ, get_beijing_now


class AuthService:
    """Encapsulates authentication related domain logic."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    async def _authenticate(self, subject: str, password: str, *, by_email=False, ip=None):
        from app.services.login_protection_service import LoginProtectionService
        from common.models.user import UserStatus
        column = User.email if by_email else User.username
        user = (await self.session.execute(select(User).where(func.lower(column) == subject.lower()))).scalar_one_or_none()
        identity = user.username if user else subject
        guard = LoginProtectionService(self.session)
        error = "登录信息有误或暂时受限，请稍后重试"
        if await guard.check(identity, ip):
            await self.session.commit()
            return None, error
        success = bool(user and user.status == UserStatus.ACTIVE and self._verify_user_password(user, password))
        await guard.record(identity, ip, success=success)
        if not success:
            return None, error
        # 旧字段仅保留兼容展示，不再叠加旧的三次/两小时规则。
        user.login_fail_count = 0
        user.login_locked_until = None
        return user, None

    async def authenticate_by_username(self, username: str, password: str, ip=None):
        return await self._authenticate(username, password, ip=ip)

    async def authenticate_by_email(self, email: str, password: str, ip=None):
        return await self._authenticate(email, password, by_email=True, ip=ip)

    def _verify_user_password(self, user: User, password: Optional[str]) -> bool:
        if not password:
            return False
        stored_hash = (user.password_hash or "").strip()
        if len(stored_hash) == 64 and all(c in "0123456789abcdefABCDEF" for c in stored_hash):
            return sha256(password.encode("utf-8")).hexdigest() == stored_hash.lower()
        return security.verify_password(password, stored_hash)

    async def mark_login(self, user: User) -> None:
        user.last_login_at = get_beijing_now()
        await self.session.flush()
        await self.session.commit()

    def create_access_token(self, user: User) -> str:
        payload = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role.value,
            "token_version": user.token_version or 0,
        }
        return security.create_access_token(
            payload,
            expires_delta=timedelta(minutes=self.settings.access_token_expire_minutes),
        )

    def create_refresh_token(self, user: User) -> str:
        """创建刷新令牌"""
        payload = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role.value,
            "token_version": user.token_version or 0,
        }
        return security.create_refresh_token(
            payload,
            expires_delta=timedelta(minutes=self.settings.refresh_token_expire_minutes),
        )
