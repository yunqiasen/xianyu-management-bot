"""一次性提交内容并核实明确回执；无回执不是发送成功。"""
import asyncio

async def receipt(result,timeout):
    if not isinstance(result,dict): return 'unknown'
    if result.get('delivery_state') in {'confirmed','not_sent'}: return result['delivery_state']
    future=result.get('send_future')
    if future is None: return 'unknown'
    try: response=await asyncio.wait_for(asyncio.shield(future),timeout)
    except Exception: return 'unknown'
    if not isinstance(response,dict): return 'unknown'
    if (response.get('body') or {}).get('reason'): return 'not_sent'
    code=response.get('code')
    return 'confirmed' if code in (200,'200') else 'unknown'

async def send_payload(payload,send_text,send_image,*,timeout=8,record=None):
    states=[]
    async def submit(part,callback,value):
        if record: await record(part,'sending')
        try: state=await receipt(await callback(value),timeout)
        except Exception: state='unknown'
        if record: await record(part,state)
        states.append(state)
    for index,text in enumerate(payload.get('texts',[])):
        for segment,part in enumerate(text.split('######')):
            if part.strip(): await submit(f'text:{index}:{segment}',send_text,part.strip())
    for index,image in enumerate(payload.get('images',[])):
        await submit(f'image:{index}',send_image,image)
    if states and all(s=='confirmed' for s in states): return 'confirmed'
    if states and all(s=='not_sent' for s in states): return 'not_sent'
    return 'unknown'
