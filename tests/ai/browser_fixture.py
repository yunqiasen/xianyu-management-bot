"""浏览器 S1/S2 演练：仅内存库、固定后台身份、本地模型 fixture。"""
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.api.routes import ai
from app.api import deps
from app.services.account_service import AccountService
from app.services.ai_reply_service import AIReplySettingsService
from common.models.xy_account import XYAccount
from common.models.ai_preset import AIPreset
from common.models.user import UserRole

@asynccontextmanager
async def lifespan(app):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(XYAccount.__table__.create)
        await conn.run_sync(AIPreset.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        for pk in (1, 2):
            session.add(XYAccount(id=pk, owner_id=7, account_id=f'account-{pk}', cookie='fixture', login_method='manual', metadata_json={'ai_reply_settings': {'model_name': 'fixture', 'api_key': 'fixture-secret', 'base_url': f'http://127.0.0.1:{os.environ["AI_FIXTURE_PORT"]}/fixture/v1'}}))
        await session.commit()
    async def db():
        async with sessions() as session:
            yield session
    app.dependency_overrides[deps.get_db_session] = db
    app.dependency_overrides[deps.get_current_active_user] = lambda: SimpleNamespace(id=7, role=UserRole.MEMBER)
    yield
    await engine.dispose()

app = FastAPI(lifespan=lifespan)
app.include_router(ai.router, prefix='/api/v1/ai-reply-settings')
app.include_router(ai.test_router, prefix='/api/v1')
@app.post('/fixture/v1/responses')
async def responses(request: Request):
    body = await request.json()
    return {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '本地 Responses 测试成功'}]}]}
@app.post('/fixture/v1/chat/completions')
async def completions(request: Request):
    return {'choices': [{'message': {'content': '本地模型测试成功'}}]}
