"""旧发货入口的持久桥：None 仅代表无持久意图的上游专用链。"""
from sqlalchemy import select
from common.db.session import async_session_maker
from common.models.xy_order import XYOrder
from common.models.xy_account import XYAccount
from common.models.delivery_intent import DeliveryIntent
from common.services.delivery_execution import DeliveryExecution
from common.services.order_delivery_runtime import OrderDeliveryRuntime
from common.services.card_purchase import purchase_card
from common.services.delivery_transport import send_payload


def response(row, group=None):
    group=group or [row]
    complete=all(entry.content_state=='confirmed' for entry in group)
    content_state='confirmed' if complete else ('unknown' if any(entry.content_state in {'unknown','purchasing','sending'} for entry in group) else 'reserved')
    done=complete and row.confirm_state in {'confirmed','not_required'}
    return {'success':done,'code':200,'message':'履约完成' if done else '履约阶段已保存，待继续或核实',
            'data':{'id':row.id,'order_no':row.order_no,'content_state':content_state,
                    'confirm_state':row.confirm_state,'only_send_card':row.mode=='card_only',
                    'quantity_sent':sum(entry.quantity for entry in group if entry.content_state=='confirmed'),
                    'lines':[{'line_id':entry.line_id,'intent_id':entry.id,'content_state':entry.content_state} for entry in group]}}


def rejected(message, code=409):
    return {'success': False, 'code': code, 'message': message, 'data': None}


async def maybe_deliver(request, *, live=None, pre_check_result=None):
    from app.services.xianyu.cookie_manager import get_manager
    from common.db.compat import db_manager
    async with async_session_maker() as session:
        stmt = select(XYOrder).where(XYOrder.order_no == request.order_no)
        for key in ('owner_id', 'account_id'):
            value = getattr(request, key, None)
            if value is not None: stmt = stmt.where(getattr(XYOrder, key) == value)
        orders = (await session.scalars(stmt)).all()
        if len(orders) != 1: return rejected('订单身份不唯一' if orders else '订单不存在', 409 if orders else 404)
        order = orders[0]
        if any(not getattr(order, key) or getattr(request, key, None) != getattr(order, key)
               for key in ('item_id', 'buyer_id', 'chat_id')):
            return rejected('订单买家、商品或会话身份不匹配')
        if order.chat_id.startswith('FAILED_'): return rejected('订单会话待核实')
        if hasattr(request, 'quantity'): request.quantity = order.quantity
        if hasattr(request, 'is_bargain'): request.is_bargain = bool(order.is_bargain)
        account = await session.scalar(select(XYAccount).where(XYAccount.owner_id == order.owner_id, XYAccount.account_id == order.account_id))
        if not account: return rejected('账号归属不匹配')
    runtime = OrderDeliveryRuntime(async_session_maker)
    intents = await runtime.payment_intents(order.owner_id,order.account_id,order.order_no)
    intent = intents[0] if intents else None
    live = live or get_manager().instances.get(order.account_id)
    if not live or not live.ws: return rejected('账号未连接')
    execution = DeliveryExecution(async_session_maker)
    if not await execution._allowed(live._check_account_execution): return rejected('账号执行权已失效')
    handler = live.auto_delivery_handler
    # 先检查意图，不让卡券/专用配置变化绕过旧发送事实。
    if any(entry.content_state not in {'reserved','confirmed'} or entry.confirm_state in {'unknown','confirming'} for entry in intents):
        return response(intent,intents)
    if not intent:
        rules = await runtime.preview(order.owner_id, order.account_id, order.order_no)
        selected = getattr(request, 'card_id', None)
        matches = [r for r in rules if r['matched'] and (selected is None or r['card_id'] == selected)]
        if not matches:
            # 对接卡仅放行已属于当前用户商品的明确对接关系。
            if not rules:
                from common.models.card import Card
                from common.models.card_item_relation import CardItemRelation
                async with async_session_maker() as session:
                    stmt=select(Card).join(CardItemRelation,CardItemRelation.card_id==Card.id).where(
                        CardItemRelation.user_id==order.owner_id,CardItemRelation.item_id==order.item_id,
                        CardItemRelation.source.in_(['dock_l1','dock_l2']),Card.enabled.is_(True))
                    if selected is not None: stmt=stmt.where(Card.id==selected)
                    cards=(await session.scalars(stmt)).unique().all()
                    cards=[card for card in cards if not card.is_multi_spec or (order.spec_name and order.spec_value and
                        (card.spec_name,card.spec_value)==(order.spec_name,order.spec_value))]
                    if len(cards)==1: return await _special_boundary(order)
            return rejected('卡券归属、SKU或规则未匹配')
        if len({r.get('line_id','main') for r in matches})!=len(matches): return rejected('每个订单明细应匹配唯一卡券')
        rule = matches[0]
        agree = handler.get_agree_deliver_config()
        if any(r['form_only'] or r['card_type'] not in {'text','data','api','image'} for r in matches) or (agree.get('enabled') and agree.get('pickup_url')):
            if len(matches)>1: return rejected('多明细专用履约请先逐项核实')
            return await _special_boundary(order)
    # 已发出的内容只补确认，跳过会产生关闭/提示副作用的发送预检。
    if not intents or any(entry.content_state=='reserved' for entry in intents):
        if not db_manager.get_item_info(order.account_id, order.item_id): return rejected('商品不属于当前账号')
        if not await handler._ensure_order_amount_before_delivery(order.order_no, order.item_id, order.buyer_id):
            return rejected('订单金额或详情待核实')
        pre = pre_check_result if pre_check_result is not None else await handler.pre_delivery_check_and_close(websocket=live.ws, order_no=order.order_no,
            buyer_id=order.buyer_id, chat_id=order.chat_id, item_id=order.item_id)
        if pre.get('action', 'allow') == 'block': return rejected('发货规则已暂停此订单')
        closed = pre.get('action') == 'card_only'
        if intent and closed and intent.mode != 'card_only': return rejected('订单关闭后需核实原履约')
        if not intent:
            mode = 'card_only' if closed or handler.is_only_send_card_enabled() else ('send_first' if handler.is_send_before_confirm_enabled() else 'confirm_first')
            try:
                intents=await execution.reserve_batch(order.owner_id,order.account_id,order.order_no,matches,mode=mode,allow_closed=closed)
                intent=intents[0]
            except (ValueError, PermissionError) as exc: return rejected(str(exc))
    async def confirm(number):
        from app.services.shipping.commerce_confirmation import confirm_order
        return await confirm_order(execution,intent,order,handler,live._check_account_execution)
    async def purchase(config, **kwargs):
        return await purchase_card(config,connector=live._build_session_connector(),**kwargs)
    result=await runtime.execute_group(intents,
        send_text=lambda text:handler.send_msg(live.ws,order.chat_id,order.buyer_id,text),
        send_image=lambda image:handler.send_image_msg(live.ws,order.chat_id,order.buyer_id,image),
        confirm=confirm if handler.is_auto_confirm_enabled() else None,
        purchase=purchase,check=live._check_account_execution)
    return response(result[0],result)

async def _special_boundary(order):
    # 上游专用链仍有裸订单号读取；有跨账号同名时不放行给旧读取器。
    async with async_session_maker() as session:
        rows=(await session.scalars(select(XYOrder.id).where(XYOrder.order_no==order.order_no))).all()
        if len(rows)!=1: return rejected('专用链订单身份不唯一，待核实')
    return None
