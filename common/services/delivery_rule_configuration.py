"""Project the legacy account credit rule into the rule table in the caller's transaction."""
from sqlalchemy import select
from common.models.xy_delivery_block_rule import XYDeliveryBlockRule
from common.services.typed_settings import validate_value


async def sync_legacy_credit_rule(session, account):
    reason = validate_value('delivery_disabled_reason', account.delivery_disabled_reason)
    enabled = bool(account.delivery_disabled)
    auto_close = bool(account.auto_close_order) if enabled else False
    excluded = account.delivery_disabled_excluded_items or []
    if not isinstance(excluded, list):
        raise ValueError('invalid_excluded_item_ids')
    excluded = list(dict.fromkeys(str(value).strip() for value in excluded
                                 if value is not None and str(value).strip()))[:500]
    values = {
        'delivery_disabled': enabled,
        'delivery_disabled_reason': (reason.strip() or None) if enabled and reason else None,
        'auto_close_order': auto_close,
        'delivery_only_card_after_close': bool(account.delivery_only_card_after_close) if auto_close else False,
        'delivery_disabled_excluded_items': (excluded or None) if enabled else None,
    }
    changed = [key for key, value in values.items() if getattr(account, key) != value]
    for key, value in values.items():
        setattr(account, key, value)
    rule = await session.scalar(select(XYDeliveryBlockRule).where(
        XYDeliveryBlockRule.account_id == account.account_id,
        XYDeliveryBlockRule.rule_code == 'buyer_credit_zero').with_for_update())
    if rule is None:
        rule = XYDeliveryBlockRule(account_id=account.account_id, rule_code='buyer_credit_zero',
            priority=10, config={'threshold': 0})
        session.add(rule)
    rule.enabled = enabled
    rule.block_reason = account.delivery_disabled_reason
    rule.auto_close_order = auto_close
    rule.only_card_after_close = account.delivery_only_card_after_close
    rule.excluded_item_ids = account.delivery_disabled_excluded_items
    return changed
