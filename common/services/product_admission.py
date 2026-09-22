"""Read the accounts agent's runtime status without modifying account credentials.

This is a preflight only. Cross-process execution fencing and budget reservation
must wrap external calls using the shared accounts executor at integration time.
"""
def product_admission(account, *, now=None):
    import time
    now = time.time() if now is None else now
    if account.status != 'active':
        return {'allowed': False, 'status': 'disabled', 'message': '账号已停用'}
    runtime = (getattr(account, 'metadata_json', None) or {}).get('xy_runtime')
    if runtime is None:
        return {'allowed': True, 'status': 'legacy'}
    state = runtime.get('business_state', 'unchecked')
    retry = runtime.get('next_retry_at', 0)
    if state != 'ready' or runtime.get('recovery_running') or retry > now:
        return {'allowed': False, 'status': state if state != 'ready' else 'cooldown',
                'next_retry_at': retry, 'message': f'账号业务等待：{state}'}
    return {'allowed': True, 'status': 'ready', 'credential_version': runtime.get('credential_version', 0),
            'generation': runtime.get('generation', 0)}
