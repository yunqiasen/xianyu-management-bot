"""S3 additive migration. Run via AsyncConnection.run_sync before app workers start.

No account switches, credentials, product rows or existing logs are rewritten.
"""
def upgrade_products(connection):
    from sqlalchemy import inspect, text
    from common.models.product_polish_schedule import ProductPolishSchedule
    ProductPolishSchedule.__table__.create(connection, checkfirst=True)
    from common.models.product_operation import ProductPublishBatch, ProductOperationEvidence
    for model in (ProductPublishBatch, ProductOperationEvidence):
        model.__table__.create(connection, checkfirst=True)
    from common.models.product_feedback import ProductFeedbackAttempt, ProductRateTemplate
    for model in (ProductFeedbackAttempt, ProductRateTemplate):
        model.__table__.create(connection, checkfirst=True)
    from common.models.product_polish_run import ProductPolishRun
    ProductPolishRun.__table__.create(connection, checkfirst=True)
    schema = inspect(connection)
    columns={c['name'] for c in schema.get_columns('xy_product_polish_schedules')}
    for name,definition in [('randomize','BOOLEAN NOT NULL DEFAULT 0'),('planned_cycle','VARCHAR(100) NULL'),('planned_at','BIGINT NULL')]:
        if name not in columns:
            connection.execute(text(f'ALTER TABLE xy_product_polish_schedules ADD COLUMN {name} {definition}'))
    if schema.has_table('xy_publish_logs'):
        columns = {column['name'] for column in schema.get_columns('xy_publish_logs')}
        if 'publish_snapshot' not in columns:
            connection.execute(text('ALTER TABLE xy_publish_logs ADD COLUMN publish_snapshot JSON NULL'))
