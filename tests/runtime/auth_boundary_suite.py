import sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[2]/'backend-web'),str(Path(__file__).resolve().parents[2])]
from fastapi import FastAPI,Depends
from httpx import AsyncClient,ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.api.deps import get_current_user,get_db_session
from app.core.security import create_access_token,create_refresh_token
from common.models import User,UserStatus

class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine=create_async_engine('sqlite+aiosqlite:///:memory:');self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        async with self.engine.begin() as conn:await conn.run_sync(User.__table__.create)
        async with self.sessions() as s:
            s.add(User(id=1,username='fixture',email='fixture@example.test',password_hash='fixture'));await s.commit()
        async def db():
            async with self.sessions() as s:yield s
        self.app=FastAPI();self.app.dependency_overrides[get_db_session]=db
        @self.app.get('/identity')
        async def identity(user=Depends(get_current_user)):return {'user_id':user.id}
        self.client=AsyncClient(transport=ASGITransport(app=self.app),base_url='http://test')
    async def asyncTearDown(self):await self.client.aclose();await self.engine.dispose()
    async def request(self,token):return await self.client.get('/identity',headers={'Authorization':'Bearer '+token})
    async def test_refresh_is_not_an_access_token(self):
        self.assertEqual((await self.request(create_refresh_token('1'))).status_code,401)
        self.assertEqual((await self.request(create_access_token('1'))).status_code,200)
    async def test_disabled_user_and_non_numeric_identity_are_rejected(self):
        self.assertEqual((await self.request(create_access_token('not-a-user'))).status_code,401)
        async with self.sessions() as s:
            u=await s.get(User,1);u.status=UserStatus.INACTIVE;await s.commit()
        self.assertEqual((await self.request(create_access_token('1'))).status_code,401)

    async def test_password_revision_revokes_old_access_token(self):
        token=create_access_token({'sub':'1','token_version':0})
        async with self.sessions() as session:
            user=await session.get(User,1)
            user.token_version=1
            await session.commit()
        self.assertEqual((await self.request(token)).status_code,401)
        self.assertEqual((await self.request(create_access_token({'sub':'1','token_version':1}))).status_code,200)

if __name__=='__main__':unittest.main()
