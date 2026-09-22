"""S1: uploaded media, static previews and logout use the same current owner."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend-web'), str(ROOT)]
ASSETS = tempfile.TemporaryDirectory(prefix='xymb-private-assets-')
os.environ['STATIC_DIR'] = ASSETS.name
from PIL import Image
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from httpx import AsyncClient, ASGITransport
from app.api import deps
from app.core.security import create_access_token, decode_token
from common.models.user import User
from common.models.xy_account import XYAccount
from common.models.confirm_receipt_message import ConfirmReceiptMessage
import _bootstrap


class AssetPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(ASSETS.name)
        image = io.BytesIO(); Image.new('RGB', (2, 2), 'green').save(image, 'PNG')
        self.image = image.getvalue()
        for owner in (1, 2):
            folder = self.root / 'uploads/products' / str(owner)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'fixture.png').write_bytes(self.image)
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            for table in (User.__table__, XYAccount.__table__, ConfirmReceiptMessage.__table__):
                await connection.run_sync(table.create)
        async with self.sessions() as db:
            db.add_all([User(id=i, username=f'owner{i}', email=f'owner{i}@example.test', password_hash='fixture') for i in (1, 2)])
            db.add_all([XYAccount(id=i, owner_id=i, account_id=f'a{i}', unb=str(i), cookie=f'unb={i}', login_method='manual', status='active') for i in (1, 2)])
            await db.commit()
        async def database():
            async with self.sessions() as db: yield db
        _bootstrap.app.dependency_overrides[deps.get_db_session] = database
        self.factory = patch('common.db.session.async_session_maker', self.sessions)
        self.factory.start()
        self.client = AsyncClient(transport=ASGITransport(app=_bootstrap.app, raise_app_exceptions=False), base_url='http://fixture')
        self.url = '/static/uploads/products/1/fixture.png'

    async def asyncTearDown(self):
        await self.client.aclose()
        self.factory.stop()
        _bootstrap.app.dependency_overrides.clear()
        await self.engine.dispose()

    def headers(self, owner=1):
        return {'Authorization': 'Bearer ' + create_access_token({'sub': str(owner), 'token_version': 0})}

    async def test_static_media_needs_current_owner_not_just_a_guessed_filename(self):
        hidden = await self.client.get(self.url)
        self.assertNotEqual(hidden.status_code, 200)
        hidden = await self.client.get(self.url, headers=self.headers(2))
        self.assertEqual(hidden.status_code, 404)
        visible = await self.client.get(self.url, headers=self.headers())
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.content, self.image)
        self.assertEqual(visible.headers['cache-control'], 'no-store')

    async def test_verify_sets_preview_only_cookie_and_logout_revokes_both(self):
        auth = self.headers()
        result = await self.client.get('/api/v1/auth/verify', headers=auth)
        self.assertTrue(result.json()['authenticated'])
        cookie = self.client.cookies.get('xymb_media')
        self.assertIsNotNone(cookie)
        self.assertEqual(decode_token(cookie)['type'], 'asset')
        self.assertIn('HttpOnly', result.headers['set-cookie'])
        self.assertEqual((await self.client.get(self.url)).status_code, 200)
        # A preview cookie is never an API access credential.
        invalid = await self.client.get('/api/v1/auth/verify', headers={'Authorization':'Bearer '+cookie})
        self.assertFalse(invalid.json()['authenticated'])
        self.assertTrue((await self.client.post('/api/v1/auth/logout', headers=auth)).json()['success'])
        self.assertFalse((await self.client.get('/api/v1/auth/verify', headers=auth)).json()['authenticated'])
        self.assertNotEqual((await self.client.get(self.url, headers=auth)).status_code, 200)

    async def test_product_upload_records_owner_in_reference(self):
        result = await self.client.post('/api/v1/product-publish/upload/images', headers=self.headers(),
                                       files=[('files', ('fixture.png', self.image, 'image/png'))])
        self.assertTrue(result.json()['success'], result.text)
        data = result.json()['data']
        self.assertTrue(data['urls'][0].startswith('/static/uploads/products/1/'))
        self.assertEqual((await self.client.get(data['urls'][0], headers=self.headers(2))).status_code, 404)
        self.assertEqual((await self.client.get(data['urls'][0], headers=self.headers())).content, self.image)

    async def test_symlink_and_face_screenshot_are_not_static_shortcuts(self):
        link = self.root / 'uploads/products/1/other.png'
        link.unlink(missing_ok=True); link.symlink_to(self.root / 'uploads/products/2/fixture.png')
        face = self.root / 'uploads/face'; face.mkdir(exist_ok=True)
        (face / 'fixture.png').write_bytes(self.image)
        for url in ('/static/uploads/products/1/other.png', '/static/uploads/face/fixture.png'):
            self.assertEqual((await self.client.get(url, headers=self.headers())).status_code, 404)

    async def test_receipt_settings_and_upload_cannot_cross_account_owner(self):
        base = '/api/v1/confirm-receipt-messages/a2'
        for result in (await self.client.get(base, headers=self.headers()),
                       await self.client.put(base, headers=self.headers(), json={'message_content':'fixture'}),
                       await self.client.post(base+'/upload-image', headers=self.headers(),
                           files={'image':('f.png',self.image,'image/png')})):
            self.assertEqual(result.json().get('code'), 404, result.text)

    async def test_existing_image_upload_forms_return_owned_preview_references(self):
        for url in ('/api/v1/cards/upload-image', '/api/v1/default-replies/a1/upload-image',
                    '/api/v1/confirm-receipt-messages/a1/upload-image', '/api/v1/upload/upload-image'):
            response = await self.client.post(url, headers=self.headers(),
                files={'image':('fixture.png',self.image,'image/png')})
            payload = response.json()
            self.assertTrue(payload.get('success'), payload)
            reference = payload.get('image_url') or payload['data']['image_url']
            self.assertEqual((await self.client.get(reference, headers=self.headers(2))).status_code, 404)
            self.assertEqual((await self.client.get(reference, headers=self.headers())).status_code, 200)

    async def test_publish_video_rejects_other_owner_before_rpc(self):
        from common.services.xianyu_publish_video import upload_publish_video, PublishVideoError
        video = self.root / 'uploads/products/2/fixture.mp4'
        video.write_bytes(b'fixture-video')
        with patch('common.services.video_gateway.upload_account_video', AsyncMock()) as rpc:
            with self.assertRaises(PublishVideoError):
                await upload_publish_video({'path':str(video)}, '', 'a1', 1, static_root=self.root)
        rpc.assert_not_awaited()

    async def test_publish_does_not_read_another_owners_local_image(self):
        from common.services.xianyu_publish_media import upload_publish_image, PublishMediaError
        rpc = AsyncMock(return_value={'url':'https://img.alicdn.com/f.png','width':2,'height':2})
        with patch('common.services.image_gateway.upload_account_image_content', rpc):
            for value in (str(self.root / 'uploads/products/2/fixture.png'),
                          '/static/uploads/products/1/../2/fixture.png'):
                with self.assertRaises(PublishMediaError):
                    await upload_publish_image(value, '', static_root=self.root, account_id='a1', owner_id=1)
        rpc.assert_not_awaited()


if __name__ == '__main__':
    try: unittest.main()
    finally: ASSETS.cleanup()
