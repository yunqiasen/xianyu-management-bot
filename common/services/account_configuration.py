"""Versioned account settings; saving and consumer application are separate facts."""
import math
from pydantic import BaseModel, ConfigDict, Field, model_validator
from common.services import account_policy
from common.services.account_request_budget import BudgetError, RequestBudgetPolicy, account_risk_config


class RequestPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_config_version: int = Field(ge=0)
    min_interval_seconds: float = Field(gt=0, le=86400, allow_inf_nan=False)
    max_interval_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    requests_per_minute: float | None = Field(default=None, gt=0, le=60000, allow_inf_nan=False)

    @model_validator(mode='after')
    def validate_interval(self):
        if self.max_interval_seconds is not None and self.max_interval_seconds < self.min_interval_seconds:
            raise ValueError('maximum_interval_below_minimum')
        return self

    def risk_values(self):
        return self.model_dump(exclude={'expected_config_version'}, exclude_none=True)


def request_policy_view(account):
    state = account_policy.snapshot(account)
    values = account_risk_config(account)
    error, interval = None, None
    try:
        interval = RequestBudgetPolicy.from_risk_config(values).interval_ms / 1000
    except BudgetError as exc:
        error = exc.code
    return {'configured': error is None, 'error': error,
            'values': {key: value for key, value in (values or {}).items()
                       if key in {'min_interval_seconds', 'max_interval_seconds', 'requests_per_minute'}
                       and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)}
                      if isinstance(values, dict) else {},
            'effective_interval_seconds': interval,
            'source': state.get('config_sources', {}).get('risk', 'default') if 'risk' in state.get('inherited_fields', []) else ('account' if values is not None else 'unset'),
            'config_version': state['config_version'], 'pending_consumers': account_policy.pending_consumers(account)}


class ConfigurationError(RuntimeError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


class ConfigurationVersion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    config_version: int = Field(ge=0)


class ConfigurationApplyRequest(ConfigurationVersion):
    owner_id: int = Field(gt=0)
    account_id: str = Field(min_length=1, max_length=80)


class ConfigurationStore:
    def __init__(self, sessions=None):
        if sessions is None:
            from common.db.session import async_session_maker
            sessions = async_session_maker
        self.sessions = sessions

    async def _account(self, session, owner_id, account_id, version, *, lock=False):
        from sqlalchemy import select
        from common.models.xy_account import XYAccount
        query = select(XYAccount).where(XYAccount.owner_id == owner_id, XYAccount.account_id == account_id)
        if lock:
            query = query.with_for_update()
        account = await session.scalar(query.execution_options(populate_existing=True))
        if account is None or account.status == 'deleted':
            raise ConfigurationError('account_not_found', 404)
        if account_policy.snapshot(account)['config_version'] != version:
            raise ConfigurationError('stale_configuration')
        return account

    async def read(self, owner_id, account_id, version):
        async with self.sessions() as session:
            return await self._account(session, owner_id, account_id, version)

    async def acknowledge(self, owner_id, account_id, version, consumer, mode='read_through'):
        async with self.sessions() as session:
            account = await self._account(session, owner_id, account_id, version, lock=True)
            account_policy.ack_config(account, consumer, version)
            await session.commit()
        return {'consumer':consumer, 'config_version':version, 'applied':True, 'status':mode}


class ConfigurationClient:
    def __init__(self, urls, token):
        from common.utils.internal_auth import build_internal_auth_headers
        self.urls, self.headers = urls, build_internal_auth_headers(token)

    async def apply(self, request, consumers):
        import asyncio
        import httpx
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=25) as http:
            async def apply_one(name):
                failed = {'consumer':name, 'config_version':request.config_version, 'applied':False}
                try:
                    response = await http.post(self.urls[name].rstrip('/') + '/internal/account-configuration',
                                               json=request.model_dump(), headers=self.headers)
                    if response.status_code != 200:
                        return {**failed, 'status':'consumer_unavailable'}
                    body = response.json()
                    if (not isinstance(body, dict) or body.get('consumer') != name
                            or body.get('config_version') != request.config_version or body.get('applied') is not True):
                        return {**failed, 'status':'application_unverified'}
                    return {'consumer':name, 'config_version':request.config_version, 'applied':True}
                except (httpx.HTTPError, ValueError, KeyError):
                    return {**failed, 'status':'consumer_unavailable'}
            return await asyncio.gather(*(apply_one(name) for name in consumers if name in self.urls))
