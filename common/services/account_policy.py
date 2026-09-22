"""账号策略：存储在既有 metadata.xy_runtime；调用方持账号行锁并提交事务。

纯状态转换不执行网络；外部结果经凭据版本、任务身份、截止时间检查后写回。
"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import secrets
import time
from urllib.parse import quote

RUNTIME_KEY = 'xy_runtime'
CONSUMERS = ('web', 'websocket', 'scheduler')
INACTIVE = {'inactive', 'disabled', 'suspended', 'deleted'}
TERMINAL = {'verified', 'invalid', 'cancelled', 'expired', 'superseded'}


class StaleAccountOperation(RuntimeError):
    pass


def snapshot(account):
    state = deepcopy((account.metadata_json or {}).get(RUNTIME_KEY) or {})
    defaults = dict(credential_version=0, config_version=0, generation=0,
                    business_state='unchecked', reason=None, last_success_at=None,
                    recovery_attempts=0, recovery_running=False, next_retry_at=0,
                    invalidations=[], consumers={}, jobs={})
    return {**defaults, **state}


def store(account, state):
    account.metadata_json = {**(account.metadata_json or {}), RUNTIME_KEY: deepcopy(state)}


def _secret(value):
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    if set(text) <= set('*•●') or text in {'[REDACTED]', '[REDACTED_SECRET]', '******** (已保存)'}:
        return None
    return str(value)


def update_login_fields(account, *, username=None, login_password=None, clear_fields=()):
    if set(clear_fields) - {'username', 'login_password', 'proxy_pass'}:
        raise ValueError('清除字段不在允许列表')
    for field, value in [('username', username), ('login_password', login_password)]:
        if field in clear_fields:
            setattr(account, field, None)
        elif _secret(value) is not None:
            setattr(account, field, value)
    if 'proxy_pass' in clear_fields:
        account.proxy_pass = None


def replace_credentials(account, cookie, *, expected_version=None):
    state = snapshot(account)
    if expected_version is not None and state['credential_version'] != expected_version:
        raise StaleAccountOperation('凭据已更新，旧任务结果已失效')
    parts = dict(pair.strip().split('=', 1) for pair in cookie.split(';') if '=' in pair)
    identity = parts.get('unb')
    if not identity or (account.unb and str(account.unb) != identity):
        raise ValueError('凭据账号身份不一致或缺少 unb')
    account.cookie = cookie
    account.unb = identity
    state['credential_version'] += 1
    # 保存凭据不等于连接可用，也不解除手动停用。
    state.update(business_state='unchecked', reason='credentials_updated')
    store(account, state)


def bump_config(account, values=None):
    state = snapshot(account)
    state['config_version'] += 1
    state['consumers'] = {'web': state['config_version']}
    if values is not None:
        state['config_values'] = deepcopy(values)
    state.update(business_state='config_pending', reason='configuration_changed')
    store(account, state)
    return state['config_version']


def bump_config_preserving_pause(account, values=None):
    before = snapshot(account)
    version = bump_config(account, values)
    if before['business_state'] in {'paused', 'verification_required', 'cooldown', 'proxy_error', 'recovering', 'disabled'}:
        current = snapshot(account)
        current.update(business_state=before['business_state'], reason=before['reason'])
        store(account, current)
    return version


def ack_config(account, consumer, version):
    if consumer not in CONSUMERS:
        raise ValueError('未知配置消费者')
    state = snapshot(account)
    if version != state['config_version']:
        return False
    state['consumers'][consumer] = version
    store(account, state)
    return True


def pending_consumers(account):
    state = snapshot(account)
    return [name for name in CONSUMERS if state['consumers'].get(name) != state['config_version']]


def record_invalidation(account, *, now=None):
    now = time.time() if now is None else now
    state = snapshot(account)
    state['invalidations'] = [t for t in state['invalidations'] if now - t < 1800] + [now]
    if len(state['invalidations']) >= 3:
        state.update(business_state='paused', reason='repeated_invalid_credentials', recovery_running=False)
    store(account, state)


def begin_recovery(account, reason, *, now=None, jitter=0):
    now = time.time() if now is None else now
    state = snapshot(account)
    if account.status in INACTIVE:
        return {'action': 'disabled'}
    if state['business_state'] == 'paused' or state['recovery_attempts'] >= 2:
        return {'action': 'paused'}
    if state['recovery_running']:
        return {'action': 'coalesced'}
    if now < state['next_retry_at']:
        return {'action': 'cooldown', 'next_retry_at': state['next_retry_at']}
    action = {'network': 'reconnect', 'invalid_credentials': 'password_login',
              'proxy': 'proxy_error', 'rate_limit': 'cooldown',
              'verification': 'verification_required'}.get(reason, 'paused')
    state.update(reason=reason, business_state=action)
    if action in {'reconnect', 'password_login'}:
        state['recovery_attempts'] += 1
        state['recovery_running'] = True
    if action == 'cooldown':
        state['rate_limit_count'] = state.get('rate_limit_count', 0) + 1
        delay = 60 if state['rate_limit_count'] == 1 else 300
        state['next_retry_at'] = now + delay
        if state['rate_limit_count'] >= 3:
            action = 'paused'
            state.update(business_state='paused', reason='repeated_rate_limit')
    store(account, state)
    return {'action': action, 'next_retry_at': state['next_retry_at']}


def finish_recovery(account, *, success, now=None, jitter=0):
    now = time.time() if now is None else now
    state = snapshot(account)
    state['recovery_running'] = False
    if success:
        state.update(recovery_attempts=0, next_retry_at=0, last_success_at=now,
                     business_state='disabled' if account.status in INACTIVE else 'ready', reason=None)
    else:
        state.update(next_retry_at=now + 60 * (1 + max(-.2, min(.2, jitter))),
                     business_state='paused' if state['recovery_attempts'] >= 2 else 'recovering')
    store(account, state)


def start_job(account, *, owner_id, kind, now=None, platform_expires_at=None):
    if owner_id != account.owner_id:
        raise PermissionError('账号不存在')
    if kind not in {'cookie_import', 'qr_refresh', 'password_login', 'verification'}:
        raise ValueError('未知账号任务类型')
    now = time.time() if now is None else now
    for job_id in list(snapshot(account)['jobs']):
        get_job(account, job_id, owner_id=owner_id, now=now)
    state = snapshot(account)
    for job in state['jobs'].values():
        if job['status'] not in TERMINAL:
            return deepcopy(job)  # 只合并当前绑定且仍有效的在途任务
    expires = now + (900 if kind == 'verification' else 300)
    if platform_expires_at is not None:
        expires = min(expires, platform_expires_at)
    job = dict(id=secrets.token_urlsafe(24), owner_id=owner_id, kind=kind,
               status='processing', created_at=now, expires_at=expires,
               credential_version=state['credential_version'], config_version=state['config_version'],
               generation=state['generation'])
    state['jobs'] = {k: v for k, v in state['jobs'].items() if now - v['created_at'] < 86400}
    state['jobs'][job['id']] = job
    state.update(business_state='verification_required' if kind == 'verification' else 'recovering', reason=kind)
    store(account, state)
    return deepcopy(job)


def get_job(account, job_id, *, owner_id, now=None):
    if owner_id != account.owner_id:
        raise PermissionError('账号任务不存在')
    now = time.time() if now is None else now
    state = snapshot(account)
    job = state['jobs'].get(job_id)
    if not job or job['owner_id'] != owner_id:
        raise PermissionError('账号任务不存在')
    if job['status'] not in TERMINAL:
        # 先核对三种版本，再处理截止；旧任务只结束自己，不暂停当前会话。
        if any(state[key] != job.get(key, state[key]) for key in
               ('credential_version', 'config_version', 'generation')):
            job['status'] = 'superseded'
            store(account, state)
        elif now >= job['expires_at']:
            job['status'] = 'expired'
            state.update(business_state='paused', reason='task_expired')
            store(account, state)
    return deepcopy(job)


def cancel_job(account, job_id, *, owner_id, now=None):
    job = get_job(account, job_id, owner_id=owner_id, now=now)
    if job['status'] in TERMINAL:
        return job
    state = snapshot(account)
    state['jobs'][job_id]['status'] = 'cancelled'
    state.update(business_state='paused', reason='task_cancelled')
    store(account, state)
    return deepcopy(state['jobs'][job_id])


def complete_job(account, job_id, cookie, *, now=None):
    job = get_job(account, job_id, owner_id=account.owner_id, now=now)
    if job['status'] in TERMINAL:
        return False
    state = snapshot(account)
    if (state['credential_version'] != job['credential_version'] or
            state['config_version'] != job['config_version'] or
            state['generation'] != job.get('generation', state['generation'])):
        state['jobs'][job_id]['status'] = 'superseded'
        store(account, state)
        return False
    replace_credentials(account, cookie, expected_version=job['credential_version'])
    state = snapshot(account)
    state['jobs'][job_id]['status'] = 'verified'
    state.update(last_success_at=time.time() if now is None else now,
                 business_state='disabled' if account.status in INACTIVE else 'unchecked', reason='session_verified')
    store(account, state)
    return True


def proxy_url(config):
    kind = config.get('proxy_type') or 'none'
    if kind == 'none':
        return None
    if kind not in {'http', 'https', 'socks5'}:
        raise ValueError('代理类型应为 none/http/https/socks5')
    host = str(config.get('proxy_host') or '').strip()
    port = config.get('proxy_port')
    if not host or any(c in host for c in '/@?# \\') or any(ord(c) < 33 for c in host):
        raise ValueError('代理主机无效，请填写主机而非 URL')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('代理端口范围为 1–65535')
    if ':' in host:
        ipaddress.IPv6Address(host.strip('[]'))
        host = '[' + host.strip('[]') + ']'
    user, password = config.get('proxy_user'), config.get('proxy_pass')
    if password and not user:
        raise ValueError('代理密码需要同时配置用户名')
    auth = f'{quote(str(user), safe="")}:{quote(str(password or ""), safe="")}@' if user else ''
    return f'{kind}://{auth}{host}:{port}'


def browser_proxy(config):
    url = proxy_url(config)
    if not url:
        return None
    if config.get('proxy_type') == 'socks5' and config.get('proxy_user'):
        raise ValueError('浏览器 SOCKS5 认证未受支持，请绑定本地无认证固定端口')
    clean = dict(config, proxy_user=None, proxy_pass=None)
    result = {'server': proxy_url(clean)}
    if config.get('proxy_user'):
        result.update(username=config['proxy_user'], password=config.get('proxy_pass') or '')
    return result


def fail_job(account, job_id, *, now=None):
    job = get_job(account, job_id, owner_id=account.owner_id, now=now)
    if job['status'] in TERMINAL:
        return False
    state = snapshot(account)
    stale = any(state[key] != job.get(key, state[key]) for key in
                ('credential_version', 'config_version', 'generation'))
    state['jobs'][job_id]['status'] = 'superseded' if stale else 'invalid'
    if not stale:
        state['jobs'][job_id]['reason'] = 'credential_check_failed'
        state.update(business_state='paused', reason='credential_check_failed')
    store(account, state)
    return not stale
