"""Local deletion and platform actions are separate, durable operator facts."""
from sqlalchemy import select
from common.models.product_operation import ProductOperationEvidence

async def record_item_operation(session, account, actor_id, action, result):
    session.add(ProductOperationEvidence(owner_id=account.owner_id,actor_id=actor_id,
        target=f'items:{account.account_id}',action=action,detail=result))
    await session.commit()

async def item_operation_history(session, owner_id, account_id):
    rows=(await session.execute(select(ProductOperationEvidence).where(ProductOperationEvidence.owner_id==owner_id,
        ProductOperationEvidence.target==f'items:{account_id}').order_by(ProductOperationEvidence.id.desc()).limit(100))).scalars()
    return [dict(action=r.action,actor_id=r.actor_id,detail=r.detail,created_at=r.created_at.isoformat()) for r in rows]
