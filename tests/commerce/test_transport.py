import asyncio
import unittest
from unittest.mock import AsyncMock
from common.services.delivery_transport import send_payload

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_without_receipt_is_unknown(self):
        sender=AsyncMock(return_value={'success':True})
        self.assertEqual(await send_payload({'texts':['A'],'images':[]},sender,sender,timeout=.01),'unknown')
    async def test_timeout_is_not_confirmation(self):
        future=asyncio.get_running_loop().create_future()
        sender=AsyncMock(return_value={'success':True,'send_future':future})
        self.assertEqual(await send_payload({'texts':['A'],'images':[]},sender,sender,timeout=.01),'unknown')
        self.assertFalse(future.cancelled())
    async def test_explicit_receipt_for_text_and_image(self):
        future=asyncio.get_running_loop().create_future();future.set_result({'code':200})
        sender=AsyncMock(return_value={'success':True,'send_future':future})
        self.assertEqual(await send_payload({'texts':['A######B'],'images':['image']},sender,sender),'confirmed')
        self.assertEqual(sender.call_count,3)
    async def test_partial_rejection_holds_whole_reservation(self):
        sender=AsyncMock(side_effect=[{'delivery_state':'confirmed'},{'delivery_state':'not_sent'}])
        self.assertEqual(await send_payload({'texts':['A','B'],'images':[]},sender,sender),'unknown')
