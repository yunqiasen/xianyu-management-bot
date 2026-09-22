import io
from pathlib import Path
import unittest
from PIL import Image
import test_state


class ImagesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await test_state.ReplyStateTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await test_state.ReplyStateTests.asyncTearDown(self)

    async def test_upload_validate_owner_reference_and_delete(self):
        self.assertTrue(Path('common/services/reply_images.py').exists(), '缺少图片资源入口')
        from common.services.reply_images import ReplyImages
        images = ReplyImages(self.sessions, Path(self.tmp.name) / 'uploads')
        buf = io.BytesIO()
        Image.new('RGB', (10, 10)).save(buf, format='PNG')
        image = await images.upload(1, buf.getvalue())
        again = await images.upload(1, buf.getvalue())
        self.assertEqual(image['id'], again['id'])
        self.assertEqual(await images.read(1, image['id']), buf.getvalue())
        with self.assertRaises(ValueError): await images.read(2, image['id'])
        with self.assertRaises(ValueError): await images.upload(1, b'not an image')
        await images.reference(1, image['id'], 'exclusive', 'rule')
        await images.reference(1, image['id'], 'exclusive', 'rule')
        self.assertEqual(len(await images.references(1, image['id'])), 1)
        with self.assertRaises(ValueError): await images.delete(1, image['id'])
        await images.unreference(1, image['id'], 'exclusive', 'rule')
        await images.delete(1, image['id'])
        with self.assertRaises(ValueError): await images.read(1, image['id'])

    async def test_nested_material_and_deleted_publish_snapshot_keep_image_by_owner(self):
        from common.models.product_material import ProductMaterial
        from common.models.publish_log import PublishLog
        from common.services.reply_images import ReplyImages
        from sqlalchemy import insert, update, delete
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: self.mod.metadata.create_all(c, tables=[ProductMaterial.__table__, PublishLog.__table__]))
        images = ReplyImages(self.sessions, Path(self.tmp.name) / 'uploads')
        buf = io.BytesIO(); Image.new('RGB', (4, 4)).save(buf, format='PNG')
        image = await images.upload(1, buf.getvalue())
        async with self.sessions() as session:
            await session.execute(insert(ProductMaterial).values(id=1, user_id=1, title='素材', description='', price=1,
                images=[], videos=[{'cover':image['url']}], specifications=[{'values':[{'image':image['url']}]}]))
            await session.execute(insert(ProductMaterial).values(id=2, user_id=2, title='other', description='', price=1, images=[image['url']]))
            await session.execute(insert(PublishLog).values(id=1, user_id=1, account_id='a', title='快照', material_id=1,
                publish_snapshot={'images':[image['url']]}))
            await session.commit()
        refs = await images.references(1, image['id'])
        self.assertEqual({r['source'] for r in refs}, {'material', 'publish_snapshot'})
        self.assertEqual({r['source_id'] for r in refs}, {'1'})
        with self.assertRaises(ValueError): await images.delete(1, image['id'])
        async with self.sessions() as session:
            await session.execute(update(ProductMaterial).where(ProductMaterial.id==1).values(is_deleted=True, videos=[], specifications=[]))
            await session.commit()
        self.assertEqual({r['source'] for r in await images.references(1, image['id'])}, {'publish_snapshot'})
        with self.assertRaises(ValueError): await images.delete(1, image['id'])
        async with self.sessions() as session:
            await session.execute(delete(PublishLog).where(PublishLog.id==1)); await session.commit()
        await images.delete(1, image['id']) # 他人素材不占用本用户资源
