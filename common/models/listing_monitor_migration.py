"""DEV46 additive DDL entrypoint; invoke only under the bootstrap migration lock."""
from common.models.listing_monitor_reliability import (
    ListingMonitorState, ListingMonitorPage, ListingMonitorObservation, ListingMonitorEvent,
)

MONITOR_MODELS = (ListingMonitorState, ListingMonitorPage, ListingMonitorObservation, ListingMonitorEvent)


def upgrade_monitor_schema(connection):
    for model in MONITOR_MODELS:
        model.__table__.create(connection, checkfirst=True)
