"""S1/S3: new installs use 30 days; saved choices survive initialization."""
from unittest.mock import patch
from support import DatabaseCase
from common.models.system_setting import SystemSetting
from app.services.system_setting_service import SystemSettingService


class RetentionDefaultTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.engine.begin() as conn:
            await conn.run_sync(SystemSetting.__table__.create)

    async def test_new_settings_use_thirty_days_without_overriding_saved_choice(self):
        service = SystemSettingService(self.session)
        self.assertEqual((await service.list_settings())['log.retention_days'], '30')
        await service.set_setting('log.retention_days', '7')
        self.assertEqual((await service.list_settings())['log.retention_days'], '7')


class StartupRetentionTests(DatabaseCase):
    async def test_mysql_startup_and_ui_read_the_same_default(self):
        from common.db import init_database
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from tools.verification.run import commerce_fixture
        with commerce_fixture() as url:
            engine = create_async_engine(url, hide_parameters=True)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(SystemSetting.__table__.create)
                with patch.object(init_database, 'async_session_maker', sessions):
                    await init_database.DatabaseInitializer().init_system_settings()
                    async with sessions() as session:
                        service = SystemSettingService(session)
                        self.assertEqual((await service.list_settings())['log.retention_days'], '30')
                        await service.set_setting('log.retention_days', '7')
                    await init_database.DatabaseInitializer().init_system_settings()
                    async with sessions() as session:
                        self.assertEqual((await SystemSettingService(session).list_settings())['log.retention_days'], '7')
            finally:
                await engine.dispose()
