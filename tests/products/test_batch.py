"""Legacy Web service entry delegates to the same durable common batch runner."""
import unittest
from unittest.mock import AsyncMock, patch
import test_api as fixtures
from sqlalchemy import select
from common.models.product_operation import ProductPublishBatch
from common.models.publish_log import PublishLog
from app.services.publish_execution_service import PublishExecutorService

class BatchTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.ProductApiTests.asyncSetUp
    asyncTearDown=fixtures.ProductApiTests.asyncTearDown
    material=fixtures.ProductApiTests.material

    async def test_legacy_entry_creates_durable_batch_before_runner(self):
        with patch('common.services.product_batch_service.ProductBatchService.run',new=AsyncMock(return_value={'batch_id':'durable'})) as run, \
             patch('app.services.publish_execution_service.detect_publish_account_capability',new=AsyncMock(return_value={'success':False})):
            async with self.sessions() as s:
                await PublishExecutorService(s).batch_publish(1,['a1'],[self.material()],batch_id='durable')
                row=await s.get(ProductPublishBatch,'durable')
                self.assertIsNotNone(row)
                logs=list((await s.execute(select(PublishLog))).scalars())
                self.assertEqual(len(logs),1);self.assertEqual(logs[0].status,'pending')
            run.assert_awaited_once()
