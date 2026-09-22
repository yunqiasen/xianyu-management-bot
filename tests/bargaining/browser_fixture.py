"""候选开关仅在本 fixture 进程打开；复用真实 API/回复链与本地模型。"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import insert
from test_candidate import CandidateTests
from common.models.auto_reply_message_log import XYAutoReplyMessageLog


@asynccontextmanager
async def lifespan(app):
    case = CandidateTests()
    await case.asyncSetUp()
    try:
        await case.configure(max_context_chars=1800)
        for i in range(18):
            await case.inbound(f'旧问题{i}' + '长' * 60, f'old-user-{i}')
            await case.store.record_message('account-a', 'chat', f'old-manual-{i}', 'assistant',
                f'人工承诺{i}' + '长' * 60, origin='manual')
        await case.inbound('能便宜20元吗', 'latest-buyer')
        _, diag = await case.generate('能便宜20元吗')
        async with case.fixture.api.engine.begin() as conn:
            await conn.run_sync(XYAutoReplyMessageLog.__table__.create)
        async with case.sessions() as session:
            session.add(XYAutoReplyMessageLog(account_id='account-a', owner_id=7, chat_id='chat',
                sender_user_id='buyer', context_snapshot={'ai_diagnostics': diag}))
            await session.commit()
        app.dependency_overrides.update(case.fixture.api.app.dependency_overrides)
        yield
    finally:
        await case.asyncTearDown()


app = FastAPI(lifespan=lifespan)
from app.api.routes.ai import router
app.include_router(router, prefix='/api/v1/ai-reply-settings')
