"""平台确认回调；每个免拼/确认子步骤在外发前留下事实。"""
async def confirm_order(execution, intent, order, handler, check):
    steps = ['confirm']
    if order.is_bargain:
        steps = ['freeshipping', 'confirm'] if intent.mode == 'send_first' else ['confirm', 'freeshipping']
    for step in steps:
        async def call():
            if step == 'freeshipping':
                return await handler.auto_freeshipping(order_id=order.order_no,item_id=order.item_id,buyer_id=order.buyer_id)
            return await handler.auto_confirm(order.order_no,order.item_id)
        result = await execution.confirm_step(intent.id,intent.owner_id,step,call,check=check)
        if result != 'confirmed': return result
    return 'confirmed'
