"""订单证据合并：退款/关闭保留事实，冲突另列待核实。"""
PROTECTED={'refunding','refunded','closed','cancelled','completed','shipped'}
EARLY={'pending_payment','pending_ship','pending','paid','processing'}

def merge_order_status(existing,incoming):
    current=existing.status
    if not incoming or current==incoming: return {}
    conflict=(current in PROTECTED and incoming in EARLY) or (current in {'refunding','refunded','closed','cancelled'} and incoming in {'shipped','completed'})
    if conflict:
        metadata=dict(existing.metadata_json or {})
        history=list(metadata.get('status_evidence') or [])
        evidence={'retained':current,'incoming':incoming}
        if evidence not in history: history.append(evidence)
        metadata.update(status_conflict=True,status_evidence=history[-20:])
        return {'metadata_json':metadata}
    if current in {'pending_ship','pending','paid'} and incoming=='pending_payment': return {}
    return {'status':incoming}
