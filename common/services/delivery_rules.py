"""Rule configuration and deterministic matching used by previews and fulfillment."""
import json
import unicodedata
from sqlalchemy import select
from common.models.card import Card
from common.models.delivery_rule import DeliveryRule
from common.models.xy_account import XYAccount


def normalized_spec(value):
    if value is None: return ''
    try:
        values = json.loads(value)
        if isinstance(values, list): return tuple(normalized_spec(v) for v in values)
    except (ValueError, TypeError): pass
    return ''.join(unicodedata.normalize('NFKC', str(value)).casefold().split())


def sku_matches(card, order):
    return (not card.is_multi_spec or bool(order.spec_name and order.spec_value and
            normalized_spec(card.spec_name) == normalized_spec(order.spec_name) and
            normalized_spec(card.spec_value) == normalized_spec(order.spec_value)))


_ASCII_LOWER = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def _legacy_like(value, pattern):
    """SQLite's default LIKE: ASCII folding, %/_, no implicit escape character.

    Dynamic programming bounds wildcard work to O(title * pattern); no regex
    backtracking. NUL terminates SQLite TEXT matching, as it does LENGTH().
    """
    value = value.split('\0', 1)[0].translate(_ASCII_LOWER)
    pattern = pattern.split('\0', 1)[0].translate(_ASCII_LOWER)
    previous = [True] + [False] * len(value)
    for token in pattern:
        current = [previous[0] if token == '%' else False]
        for index, char in enumerate(value, 1):
            current.append((previous[index] or current[-1]) if token == '%' else
                           previous[index - 1] and (token == '_' or token == char))
        previous = current
    return previous[-1]


def title_score(rule, title):
    """Return None for no match; a legacy reverse match may score zero."""
    if not title: return None
    if rule.match_mode == 'legacy_contains':
        length = len(rule.keyword.split('\0', 1)[0])
        if _legacy_like(title, '%' + rule.keyword + '%'): return length * 2
        if _legacy_like(rule.keyword, '%' + title + '%'): return (length // 2) * 2
        return None
    if rule.match_mode == 'exact': return len(rule.keyword) * 2 if title == rule.keyword else None
    return len(rule.keyword) * 2 if rule.keyword in title else None


def rule_view(row):
    return {key: getattr(row, key) for key in ('id','account_id','card_id','keyword','match_mode',
        'delivery_count','enabled','priority','description','delivery_times','version')}


class DeliveryRules:
    def __init__(self, sessions): self.sessions = sessions

    async def list(self, owner, account_id=None):
        async with self.sessions() as session:
            statement = select(DeliveryRule).where(DeliveryRule.owner_id == owner)
            if account_id:
                statement = statement.where((DeliveryRule.account_id == account_id) | DeliveryRule.account_id.is_(None))
            rows = (await session.scalars(statement.order_by(DeliveryRule.priority.desc(), DeliveryRule.id).limit(1001))).all()
            if len(rows) > 1000: raise ValueError('规则数量超过单次处理上限')
            return [rule_view(row) for row in rows]

    async def save(self, owner, values, *, rule_id=None, expected_version=None):
        async with self.sessions() as session, session.begin():
            if rule_id:
                rule = await session.scalar(select(DeliveryRule).where(DeliveryRule.id == rule_id,
                    DeliveryRule.owner_id == owner).with_for_update())
                if rule is None: raise PermissionError('规则不存在')
                if rule.version != expected_version: raise ValueError('配置版本已变化，请刷新后保存')
            else:
                rule = DeliveryRule(owner_id=owner)
            card_id = values.get('card_id', rule.card_id)
            card = await session.scalar(select(Card).where(Card.id == card_id, Card.user_id == owner))
            if card is None: raise PermissionError('卡券不存在')
            account_id = values.get('account_id', rule.account_id)
            if account_id and not await session.scalar(select(XYAccount.id).where(XYAccount.account_id == account_id, XYAccount.owner_id == owner)):
                raise PermissionError('账号不存在')
            for name, value in values.items(): setattr(rule, name, value)
            rule.version = (rule.version or 0) + 1
            session.add(rule)
            await session.flush()
            return rule_view(rule)


async def delivery_quantity(session, account, card, line, rule=None):
    quantity = line.quantity if card.type in {'data','api'} else 1
    if rule is not None:
        if rule.match_mode == 'legacy_contains':
            from common.models.xy_catalog_item import XYCatalogItem
            item=await session.scalar(select(XYCatalogItem).where(XYCatalogItem.owner_id==account.owner_id,
                XYCatalogItem.account_pk==account.id,XYCatalogItem.item_id==line.item_id).limit(1))
            quantity=line.quantity if item and (item.metadata_json or {}).get('multi_quantity_delivery') is True else 1
        quantity*=rule.delivery_count
    if type(quantity) is not int or not 1<=quantity<=1000: raise ValueError('发货数量超过上限')
    return quantity
