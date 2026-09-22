"""API 卡单次采购。超时由履约意图保留 unknown；不在 HTTP 层重购。"""
import json
import aiohttp
from common.services.delivery_utils import recursive_replace_params
from common.utils.response_field import extract_card_api_response_content

async def purchase_card(config,*,quantity,idempotency_key,connector=None,context=None):
    config=json.loads(config) if isinstance(config,str) else dict(config)
    headers=config.get('headers') or {};params=config.get('params') or {}
    if isinstance(headers,str): headers=json.loads(headers)
    if isinstance(params,str): params=json.loads(params)
    headers=dict(headers)
    # 仅声明支持幂等的供应商增加幂等头；无此能力也只采购一次。
    if config.get('idempotency_header'): headers[config['idempotency_header']]=idempotency_key
    params=recursive_replace_params(params,{**(context or {}),'order_id':idempotency_key,'idempotency_key':idempotency_key,'order_quantity':str(quantity),'quantity':str(quantity)})
    method=config.get('method','GET').upper()
    if method not in {'GET','POST'}: raise ValueError('供应商请求方法无效')
    timeout=aiohttp.ClientTimeout(total=min(60,max(1,float(config.get('timeout',10)))))
    async with aiohttp.ClientSession(timeout=timeout,connector=connector) as client:
        async with client.request(method,config['url'],headers=headers,**({'params':params} if method=='GET' else {'json':params})) as response:
            if response.status!=200: raise ValueError('supplier_result_unknown')
            content=extract_card_api_response_content(await response.text(),config.get('response_field') or config.get('responseField'))
    texts=content.splitlines() if quantity>1 else [content]
    if len(texts)!=quantity or any(not t.strip() for t in texts): raise ValueError('supplier_quantity_unknown')
    return {'texts':texts,'images':[]}

async def query_card(config,**kwargs):
    config=json.loads(config) if isinstance(config,str) else dict(config)
    if not config.get('query_url'): raise ValueError('供应商未配置查询接口，请人工核实；本次未重复采购')
    config={**config,'url':config['query_url'],'method':'GET'}
    return await purchase_card(config,**kwargs)
