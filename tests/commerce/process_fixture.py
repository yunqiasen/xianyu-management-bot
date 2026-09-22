"""Child process for S2 crash injection; only a named isolated MySQL schema is used."""
import argparse
import asyncio
import json
import os

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from common.services.card_purchase import purchase_card
from common.services.delivery_execution import DeliveryExecution
from common.services.delivery_transport import send_payload


def emit(**data):
    print(json.dumps(data), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account', required=True)
    parser.add_argument('--order', required=True)
    parser.add_argument('--card', required=True, type=int)
    parser.add_argument('--url', required=True)
    parser.add_argument('--phase', default='none')
    parser.add_argument('--point', default='none')
    parser.add_argument('--now', type=float)
    args = parser.parse_args()
    url = os.environ['XYMB_COMMERCE_MYSQL_URL']
    parsed = make_url(url)
    if parsed.host not in {'127.0.0.1', 'localhost'} or not (parsed.database or '').startswith('xy_commerce_fixture_'):
        raise ValueError('isolated_commerce_database_required')
    engine = create_async_engine(url, hide_parameters=True, echo=False)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service = DeliveryExecution(sessions)
    try:
        intent = await service.reserve(777, args.account, args.order, args.card)

        async def checkpoint(phase, point):
            if args.phase == phase and args.point == point:
                emit(checkpoint=point, phase=phase, intent_id=intent.id)
                await asyncio.Event().wait()

        await checkpoint('reservation', 'persisted')
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            async def text(value):
                await checkpoint('content', 'before_request')
                response = await client.post(args.url + '/content', json={'intent_id': intent.id, 'text': value})
                response.raise_for_status()
                result = response.json()
                await checkpoint('content', 'after_response')
                return result

            async def send(payload):
                return await send_payload(payload, text, text,
                    record=lambda part, state: service.record_content(intent.id, 777, part, state))

            async def purchase(config, **kwargs):
                await checkpoint('purchase', 'before_request')
                result = await purchase_card(config, **kwargs)
                await checkpoint('purchase', 'after_response')
                return result

            async def confirm(order_no):
                async def request():
                    await checkpoint('confirm', 'before_request')
                    response = await client.post(args.url + '/confirm', json={'intent_id': intent.id, 'order_no': order_no})
                    response.raise_for_status()
                    result = response.json()
                    await checkpoint('confirm', 'after_response')
                    return result
                return await service.confirm_step(intent.id, 777, 'platform', request)

            result = await service.execute(intent.id, 777, send=send, confirm=confirm, purchase=purchase, now=args.now)
        await checkpoint('done', 'persisted')
        emit(done=True, intent_id=intent.id, content_state=result.content_state, confirm_state=result.confirm_state)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        emit(error=type(exc).__name__)
        raise SystemExit(1)
