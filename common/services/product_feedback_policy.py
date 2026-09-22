"""Evaluation and flower requests are order operations, not account keepalive."""
def feedback_eligibility(account, order, *, kind, cooling=False):
    if kind not in {'rate', 'red_flower'}:
        raise ValueError('未知评价动作')
    if order.owner_id != account.owner_id:
        return 'account_mismatch'
    if hasattr(order, 'account_id'):
        if order.account_id != account.account_id:
            return 'account_mismatch'
    elif getattr(order, 'account_pk', None) != account.id:
        return 'account_mismatch'
    if account.status != 'active':
        return 'disabled'
    if getattr(order, 'is_rated' if kind == 'rate' else 'is_red_flower', False):
        return 'already_processed'
    if order.status not in {'shipped', 'completed', '交易成功', '已完成', '已发货'}:
        return 'not_applicable'
    if cooling:
        return 'cooldown'
    return 'ready'
