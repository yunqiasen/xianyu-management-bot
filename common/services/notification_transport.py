"""统一发送与测试的渠道受理契约：accepted / not_accepted / unknown。"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlparse, quote
import httpx

ALIASES={'ding_talk':'dingtalk','lark':'feishu','wechat_work':'wechat'}
REQUIRED={'qq':('base_url',),'dingtalk':('webhook_url',),'feishu':('webhook_url',),'wechat':('webhook_url',),'webhook':('webhook_url',),'bark':('device_key',),'telegram':('bot_token','chat_id'),'pushplus':('token',),'email':('smtp_server','email_user','email_password','recipient_email')}

def validate_config(kind, config):
    kind=ALIASES.get(kind,kind)
    if kind not in REQUIRED or not isinstance(config,dict):
        raise ValueError('渠道类型或配置格式有误')
    for field in REQUIRED[kind]:
        if not config.get(field): raise ValueError(f'缺少配置字段: {field}')
    if kind=='qq' and bool(config.get('user_id')) == bool(config.get('group_id')):
        raise ValueError('QQ需选择一个user_id或group_id')
    for field in ('base_url','webhook_url','server_url'):
        if config.get(field) and urlparse(str(config[field])).scheme not in ('http','https'):
            raise ValueError(f'{field}需使用HTTP地址')
    if kind=='webhook' and str(config.get('http_method',config.get('method','POST'))).upper() not in ('POST','GET'):
        raise ValueError('Webhook仅支持POST或GET')
    return kind

async def deliver(kind,config,message,*,client=None):
    try: kind=validate_config(kind,config)
    except ValueError: return 'not_accepted'
    if kind=='email':
        # 旧SMTP助手只给bool；False没有未受理证据，保守保留待核实。
        from common.utils.notification_utils import send_email_notification
        return 'accepted' if await send_email_notification(config,message) else 'unknown'
    if client is None:
        async with httpx.AsyncClient(timeout=15,follow_redirects=False) as owned:
            return await deliver(kind,config,message,client=owned)
    headers={}; params=None; method='POST'; data={}
    if kind=='qq':
        group=bool(config.get('group_id')); field='group_id' if group else 'user_id'
        url=config['base_url'].rstrip('/')+('/send_group_msg' if group else '/send_private_msg')
        data={field:config[field],'message':message,'auto_escape':True}
        if config.get('access_token'): headers['Authorization']='Bearer '+config['access_token']
    elif kind in ('dingtalk','wechat','feishu'):
        url=config['webhook_url']
        data={'msgtype':'text','text':{'content':message}}
        if kind=='feishu': data={'msg_type':'text','content':{'text':message}}
        if config.get('secret') and kind=='dingtalk':
            timestamp=str(int(time.time()*1000)); secret=config['secret']
            signature=base64.b64encode(hmac.new(secret.encode(),f'{timestamp}\n{secret}'.encode(),hashlib.sha256).digest()).decode()
            params={'timestamp':timestamp,'sign':signature}
        if config.get('secret') and kind=='feishu':
            timestamp=str(int(time.time()))
            signature=base64.b64encode(hmac.new(f"{timestamp}\n{config['secret']}".encode(),b'',hashlib.sha256).digest()).decode()
            data.update(timestamp=timestamp,sign=signature)
    elif kind=='bark':
        url=config.get('server_url','https://api.day.app').rstrip('/')+'/push'
        data={k:config[k] for k in ('device_key','sound','icon','group','url') if config.get(k)}
        data.update(title=config.get('title','闲鱼通知'),body=message)
    elif kind=='telegram':
        url=f"https://api.telegram.org/bot{config['bot_token']}/sendMessage"
        data={'chat_id':config['chat_id'],'text':message}
    elif kind=='pushplus':
        url=config.get('server_url','https://www.pushplus.plus').rstrip('/')+'/send'
        data={k:config[k] for k in ('token','topic','template') if config.get(k)}
        data.update(title=config.get('title','闲鱼通知'),content=message)
    else:
        url=config['webhook_url']; method=str(config.get('http_method',config.get('method','POST'))).upper()
        headers=config.get('headers',{})
        if isinstance(headers,str):
            try: headers=json.loads(headers)
            except ValueError: return 'not_accepted'
        data={'message':message}
        if method=='GET': params=data; data=None
    try:
        response=await client.request(method,url,json=data,headers=headers,params=params)
        # 5xx可能发生在受理之后；只有明确客户端拒绝才允许自动重投。
        if response.status_code in (400,401,403,404,405,422,429): return 'not_accepted'
        if not 200 <= response.status_code < 300: return 'unknown'
        if kind=='webhook': return 'accepted'
        try: body=response.json()
        except ValueError: return 'unknown'
        if not isinstance(body,dict): return 'unknown'
        if kind=='qq':
            if body.get('status')=='ok' and body.get('retcode')==0: return 'accepted'
            return 'not_accepted' if body.get('status')=='failed' else 'unknown'
        if kind=='telegram': return 'accepted' if body.get('ok') is True else ('not_accepted' if body.get('ok') is False else 'unknown')
        key='errcode' if kind in ('dingtalk','wechat') else 'code'
        if kind=='feishu': code=body.get('code',body.get('StatusCode'))
        else: code=body.get(key)
        if code is None: return 'unknown'
        return 'accepted' if code==(200 if kind in ('bark','pushplus') else 0) else 'not_accepted'
    except (httpx.ConnectError,httpx.ConnectTimeout): return 'not_accepted'
    except Exception: return 'unknown'
