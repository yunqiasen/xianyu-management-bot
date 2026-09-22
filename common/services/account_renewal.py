"""Renewal RPC: the existing account executor owns credentials and retry budgets."""
import httpx
from pydantic import BaseModel, ConfigDict, Field
from common.services.account_dispatch import AccountDispatchClient, DispatchError
from common.services.account_policy import snapshot


class RenewalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    owner_id: int = Field(gt=0)
    account_id: str = Field(min_length=1, max_length=80)
    generation: int = Field(ge=0)
    credential_version: int = Field(ge=0)
    config_version: int = Field(ge=0)


class AccountRenewalClient(AccountDispatchClient):
    async def renew(self, account):
        state = snapshot(account)
        body = RenewalRequest(owner_id=account.owner_id, account_id=account.account_id,
            **{key:state[key] for key in ('generation','credential_version','config_version')})
        try:
            response = await self._call('POST', '/internal/account-operations/renew', json=body.model_dump())
            if response.status_code != 200:
                return {'status':'unknown' if response.status_code >= 500 else 'failed',
                        'reason':'executor_result_unverified' if response.status_code >= 500 else 'executor_rejected'}
            result = response.json()
            if result.get('status') not in {'verified','skipped','failed','unknown'}:
                raise ValueError('invalid_renewal_result')
            return {key:result.get(key,'') for key in ('status','reason')}
        except (httpx.HTTPError, ValueError):
            return {'status':'unknown','reason':'executor_result_unverified'}


async def renew_on_executor(dispatcher, request):
    from common.services.account_recovery import AccountRecovery
    from common.services.account_execution import ExecutionLost
    from common.utils.xianyu_utils import trans_cookies
    account = await dispatcher.store.account(request.owner_id,request.account_id)
    if account.status != 'active':
        return {'status':'skipped','reason':'account_disabled'}
    state = snapshot(account)
    if any(state[key] != getattr(request,key) for key in ('generation','credential_version','config_version')):
        raise DispatchError('stale_account_version',409)
    live = dispatcher.manager.instances.get(request.account_id)
    runtime = getattr(live,'_account_runtime',None)
    if runtime is None or runtime.owner_id != request.owner_id:
        raise DispatchError('executor_offline',409)
    try:
        await runtime.lease.check()
    except ExecutionLost as exc:
        raise DispatchError('execution_lost',409) from exc
    recovery = AccountRecovery(request.account_id,request.owner_id,dispatcher.store.sessions)
    expected = (request.credential_version,request.config_version,request.generation)
    job = await recovery.start('renewal',expected=expected)
    if not job:
        return {'status':'skipped','reason':'cooldown_or_paused'}
    async def renew():
        await runtime.lease.check()
        cookie = await recovery.renew_once()
        await runtime.lease.check()
        return cookie
    async def login():
        await runtime.lease.check()
        cookie = await recovery.password_once()
        await runtime.lease.check()
        return cookie
    if not await recovery.run(job['id'],renew=renew,login=login):
        return {'status':'failed','reason':'recovery_not_verified'}
    account = await dispatcher.store.account(request.owner_id,request.account_id)
    state = snapshot(account)
    if account.status != 'active' or state['generation'] != request.generation:
        return {'status':'unknown','reason':'account_changed'}
    await runtime.lease.check()
    runtime.version=(state['credential_version'],state['config_version'])
    live.cookies_str=account.cookie
    live.cookies=trans_cookies(account.cookie)
    if getattr(live,'session',None) is not None:
        live.session.headers['Cookie']=account.cookie
    return {'status':'verified','reason':''}
