"""GuDong's actual stop endpoint, plus independent durable boundary evidence.

PUT status alone is not a drained write boundary. No endpoint is guessed for
inflight evidence: the caller supplies an observed snapshot reader explicitly.
"""
import asyncio
import inspect
import json
import math
import time
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from .snapshot import MigrationError

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

class LegacyControl:
    def __init__(self, base_url, account_id, *, token, execute=False, boundary_reader=None, timeout=10):
        parsed=urlsplit(base_url)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MigrationError('legacy_control_url_invalid')
        if parsed.scheme!='https' and parsed.hostname not in {'localhost','127.0.0.1','::1'}:
            raise MigrationError('legacy_control_tls_required')
        self.base=base_url.rstrip('/'); self.account_id=account_id; self.token=token
        self.execute=execute; self.boundary_reader=boundary_reader; self.timeout=timeout

    def _request(self, method, suffix, data=None):
        url=self.base+'/cookies/'+quote(self.account_id,safe='')+suffix
        request=Request(url,method=method,data=None if data is None else json.dumps(data).encode(),
                        headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            # Ignore ambient proxies and redirects so credentials remain on the named control.
            with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=self.timeout) as response:
                raw=response.read(1024*1024+1)
                if len(raw)>1024*1024: raise ValueError()
                payload=json.loads(raw)
                if not isinstance(payload,dict): raise ValueError()
                return payload
        except Exception:
            raise MigrationError('legacy_control_request_failed') from None

    async def quiesce(self):
        if not self.execute:
            return {'stopped':False,'dryrun':True,'action':'PUT /cookies/{account}/status','watermark':None,'inflight':None}
        stopped=await asyncio.to_thread(self._request,'PUT','/status',{'enabled':False})
        if stopped.get('enabled') is not False:
            return {'stopped':False,'reason':'legacy_stop_unconfirmed'}
        runtime=await asyncio.to_thread(self._request,'GET','/runtime-status')
        if runtime.get('cookie_id')!=self.account_id: raise MigrationError('legacy_control_account_mismatch')
        live=runtime.get('runtime_status')
        if not isinstance(live,dict) or live.get('running') is not False:
            return {'stopped':False,'reason':'legacy_runtime_still_running','status_disabled':True}
        if self.boundary_reader is None:
            return {'stopped':False,'reason':'legacy_boundary_required','status_disabled':True}
        boundary=self.boundary_reader()
        if inspect.isawaitable(boundary): boundary=await boundary
        try:
            age=time.time()-float(boundary['captured_at'])
            valid=(boundary['account_id']==self.account_id and math.isfinite(age) and 0<=age<=900
                   and boundary['stopped'] is True and boundary['watermark'] is not None and isinstance(boundary['inflight'],list))
        except (KeyError,TypeError,ValueError): valid=False
        if not valid: raise MigrationError('legacy_boundary_invalid')
        return {k:boundary[k] for k in ('stopped','watermark','inflight','captured_at','account_id')}
