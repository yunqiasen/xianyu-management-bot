"""S3 additive migration replay leaves existing enabled/disabled tasks untouched."""
import importlib
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from common.models.listing_monitor_task import ListingMonitorTask


def test_additive_monitor_schema_replay_preserves_legacy_switches(tmp_path):
    try: migration = importlib.import_module('common.models.listing_monitor_migration')
    except ImportError as exc: pytest.fail(f'additive monitor migration missing: {exc}')
    engine = create_engine(f'sqlite:///{tmp_path}/migration.db')
    ListingMonitorTask.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([ListingMonitorTask(id=i, owner_id=1, keyword='fixture', is_enabled=bool(i % 2),
                   interval_minutes=1, collect_pages=3) for i in (1, 2)])
        db.commit()
    with engine.begin() as conn:
        migration.upgrade_monitor_schema(conn)
        migration.upgrade_monitor_schema(conn)
    with Session(engine) as db:
        tasks = db.execute(select(ListingMonitorTask).order_by(ListingMonitorTask.id)).scalars().all()
        assert [(t.is_enabled, t.interval_minutes, t.collect_pages) for t in tasks] == [(True, 1, 3), (False, 1, 3)]
    engine.dispose()
