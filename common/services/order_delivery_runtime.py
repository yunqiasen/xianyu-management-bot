"""Payment events and manual entry points share rule matching and durable line execution."""
from sqlalchemy import select, or_, exists
from common.models.xy_account import XYAccount
from common.models.xy_order import XYOrder
from common.models.card import Card
from common.models.card_item_relation import CardItemRelation
from common.models.delivery_intent import DeliveryIntent
from common.models.delivery_rule import DeliveryRule
from common.models.xy_catalog_item import XYCatalogItem
from common.services.delivery_execution import DeliveryExecution
from common.services.delivery_transport import send_payload
from common.services.card_purchase import purchase_card
from common.services.delivery_rules import sku_matches, title_score, delivery_quantity
from common.services.order_lines import order_lines


class OrderDeliveryRuntime:
    def __init__(self, sessions):
        self.sessions = sessions
        self.execution = DeliveryExecution(sessions)

    async def preview(self, owner, account_id, order_no):
        async with self.sessions() as session:
            order = await session.scalar(select(XYOrder).where(XYOrder.owner_id == owner,
                XYOrder.account_id == account_id, XYOrder.order_no == order_no))
            if order is None: raise PermissionError('订单不存在')
            account = await session.scalar(select(XYAccount).where(XYAccount.owner_id == owner, XYAccount.account_id == account_id))
            if account is None: raise PermissionError('账号不存在')
            output = []
            for line in order_lines(order):
                output.extend(await self._preview_line(session, account, line))
            return output

    async def _preview_line(self, session, account, line):
        cards = (await session.scalars(select(Card).where(Card.user_id == account.owner_id, or_(
            Card.item_id == line.item_id, exists(select(CardItemRelation.id).where(
                CardItemRelation.card_id == Card.id, CardItemRelation.user_id == account.owner_id,
                CardItemRelation.item_id == line.item_id, CardItemRelation.source == 'own')))))).all()
        def view(card, matched, reason, multiplier=1, **extra):
            return dict(card_id=card.id, name=card.name, card_type=card.type, matched=bool(matched), reason=reason,
                quantity=(line.quantity if card.type in {'data','api'} else 1)*multiplier,
                form_only=card.use_no_logistics_form, line_id=line.line_id, item_id=line.item_id,
                spec_name=line.spec_name, spec_value=line.spec_value, **extra)
        rows = []
        for card in cards:
            matched = card.enabled and sku_matches(card, line)
            reason = '规则已暂停' if not card.enabled else ('SKU精确命中' if card.is_multi_spec and matched else
                'SKU缺失或不匹配' if card.is_multi_spec else '通用卡券')
            rows.append(view(card, matched, reason))
        dynamic = (await session.scalars(select(DeliveryRule).where(DeliveryRule.owner_id == account.owner_id,
            or_(DeliveryRule.account_id.is_(None), DeliveryRule.account_id == account.account_id)).limit(1001))).all()
        if len(dynamic) > 1000: raise ValueError('规则数量超过处理上限')
        title = line.title
        if dynamic and not title:
            title = await session.scalar(select(XYCatalogItem.title).where(XYCatalogItem.owner_id == account.owner_id,
                XYCatalogItem.account_pk == account.id, XYCatalogItem.item_id == line.item_id).limit(1)) or ''
        dynamic_rows = []
        for rule in dynamic:
            score = title_score(rule, title)
            if score is None: continue
            card = await session.get(Card, rule.card_id)
            if card is None or card.user_id != account.owner_id: continue
            cards.append(card)
            matched = rule.enabled and card.enabled and sku_matches(card, line)
            quantity=await delivery_quantity(session,account,card,line,rule)
            dynamic_rows.append(view(card, matched, '标题规则及SKU命中' if matched else '规则停用或SKU不匹配',
                multiplier=rule.delivery_count, rule_id=rule.id, rule_version=rule.version,
                rank=(-rule.priority, -score, 0 if card.is_multi_spec else 1,
                      rule.delivery_times if card.is_multi_spec else (rule.legacy_id or 0), rule.id)))
            dynamic_rows[-1]['quantity']=quantity
        dynamic_rows.sort(key=lambda row: row.pop('rank'))
        # Missing SKU blocks generic fallback; an exact SKU beats a generic card.
        if any(card.enabled and card.is_multi_spec for card in cards) and (not line.spec_name or not line.spec_value):
            for row in rows + dynamic_rows: row.update(matched=False, reason='订单SKU缺失，暂停通用卡回退')
        exact = [row for row in rows if row['matched'] and row['reason'] == 'SKU精确命中']
        if exact:
            for row in rows:
                if row['matched'] and row not in exact: row.update(matched=False, reason='优先使用精确SKU')
        native = any(row['matched'] for row in rows)
        selected = False
        for row in dynamic_rows:
            if row['matched'] and (native or selected):
                row.update(matched=False, reason='更精确关联或更高优先级规则已命中')
            elif row['matched']: selected = True
        return rows + dynamic_rows

    async def payment_intents(self, owner, account_id, order_no):
        async with self.sessions() as session:
            return list((await session.scalars(select(DeliveryIntent).where(DeliveryIntent.owner_id == owner,
                DeliveryIntent.account_id == account_id, DeliveryIntent.order_no == order_no,
                or_(DeliveryIntent.operation_key == 'payment', DeliveryIntent.operation_key.like('payment:%'))
            ).order_by(DeliveryIntent.line_id))).all())

    async def execute_group(self, intents, *, send_text, send_image, confirm, check=None, purchase=purchase_card):
        if not intents: return []
        leader = next((row for row in intents if row.operation_key == (row.source_snapshot or {}).get('confirmation_key', row.operation_key)), intents[0])
        owner = leader.owner_id
        if leader.mode == 'confirm_first':
            await self.execution.execute(leader.id, owner, send=None, confirm=confirm, check=check)
        for intent in intents:
            async def send(payload, current=intent):
                return await send_payload(payload, send_text, send_image,
                    record=lambda part,state:self.execution.record_content(current.id, owner, part, state))
            await self.execution.execute(intent.id, owner, send=send, confirm=None, purchase=purchase, check=check)
        if leader.mode != 'confirm_first':
            await self.execution.execute(leader.id, owner, send=None, confirm=confirm, check=check)
        return await self.payment_intents(owner, leader.account_id, leader.order_no)

    async def handle(self, *, account_id, order_no, item_id, buyer_id, chat_id, send_text, send_image,
                     confirm, mode, allow_closed=False, check=None, purchase=purchase_card):
        async with self.sessions() as session:
            account = await session.scalar(select(XYAccount).where(XYAccount.account_id == account_id))
            if account is None: return True
            owner = account.owner_id
            order = await session.scalar(select(XYOrder).where(XYOrder.owner_id == owner, XYOrder.account_id == account_id, XYOrder.order_no == order_no))
            if order is None: return True
            if order.item_id != item_id or order.buyer_id != buyer_id or order.chat_id != chat_id: return True
        if not await self.execution._allowed(check): return True
        intents = await self.payment_intents(owner, account_id, order_no)
        if not intents:
            try:
                rules = await self.preview(owner, account_id, order_no)
                matches = [row for row in rules if row['matched']]
                if not matches: return False if not rules else True
                if any(row['form_only'] or row['card_type'] not in {'data','text','api','image'} for row in matches): return False
                intents = await self.execution.reserve_batch(owner, account_id, order_no, matches, mode=mode, allow_closed=allow_closed)
            except ValueError: return True
        await self.execute_group(intents, send_text=send_text, send_image=send_image, confirm=confirm, check=check, purchase=purchase)
        return True
