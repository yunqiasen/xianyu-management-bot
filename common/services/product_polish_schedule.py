"""Daily polishing admission, persisted before side effects; no missed-run catch-up."""
def polish_window(now, *, timezone_name='Asia/Shanghai', start='09:00', end='10:00', last_cycle=None):
    from datetime import datetime, time, timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone = ZoneInfo(timezone_name)
        begin, finish = time.fromisoformat(start), time.fromisoformat(end)
        if len(start) != 5 or len(end) != 5 or begin == finish:
            raise ValueError('时间窗应为不同的HH:MM')
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError('时区或时间窗格式错误') from exc
    if now.tzinfo is None:
        raise ValueError('需要带时区的当前时间')
    local = now.astimezone(zone)
    day = local.date()
    if finish < begin and local.timetz().replace(tzinfo=None) < finish:
        day -= timedelta(days=1)
    begin_at = datetime.combine(day, begin, zone)
    end_at = datetime.combine(day + timedelta(days=int(finish < begin)), finish, zone)
    cycle = f'{timezone_name}:{day.isoformat()}'
    inside = begin_at <= local < end_at
    next_run = begin_at if local < begin_at else datetime.combine(day + timedelta(days=1), begin, zone)
    status = 'already_processed' if inside and last_cycle == cycle else ('ready' if inside else 'outside_window')
    return dict(status=status, cycle=cycle, next_run_at=next_run.isoformat(), window_start=begin_at.isoformat(), window_end=end_at.isoformat())


class ProductPolishScheduleService:
    def __init__(self, session):
        self.session = session

    async def get(self, owner_id, account_id, *, locked=False):
        from sqlalchemy import select
        from common.models.product_polish_schedule import ProductPolishSchedule
        stmt=select(ProductPolishSchedule).where(ProductPolishSchedule.owner_id == owner_id, ProductPolishSchedule.account_id == account_id)
        if locked: stmt=stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def save(self, owner_id, account_id, data):
        from datetime import datetime, timezone
        from common.models.product_polish_schedule import ProductPolishSchedule
        polish_window(datetime.now(timezone.utc), **{k:data[k] for k in ('timezone_name','start','end')})
        if type(data.get('randomize',False)) is not bool: raise ValueError('随机时间窗应为布尔值')
        row = await self.get(owner_id, account_id, locked=True)
        if row is None:
            row = ProductPolishSchedule(owner_id=owner_id, account_id=account_id)
            self.session.add(row)
        if any(getattr(row,k)!=data[k] for k in ('timezone_name','start','end')) or row.randomize != data.get('randomize',False):
            row.planned_cycle=None; row.planned_at=None
        for key in ('timezone_name', 'start', 'end'):
            setattr(row, key, data[key])
        row.randomize=data.get('randomize',False)
        # Saving a new window does not reset the already consumed day.
        await self.session.commit()
        return row

    async def admit(self, account, now=None):
        from datetime import datetime, timezone
        from sqlalchemy import update
        from common.models.product_polish_schedule import ProductPolishSchedule
        if account.status != 'active' or not account.auto_polish:
            return {'status': 'disabled'}
        row = await self.get(account.owner_id, account.account_id, locked=True)
        if row is None:
            return {'status': 'legacy'}
        now = now or datetime.now(timezone.utc)
        result = polish_window(now, timezone_name=row.timezone_name,
                               start=row.start, end=row.end, last_cycle=row.last_cycle)
        if result['status'] != 'ready':
            await self.session.commit()
            return {**result, 'planned_at':row.planned_at}
        if row.planned_cycle != result['cycle'] or row.planned_at is None:
            import secrets
            begin=datetime.fromisoformat(result['window_start'])
            finish=datetime.fromisoformat(result['window_end'])
            minutes=max(1,int((finish.timestamp()-begin.timestamp())//60))
            offset=secrets.randbelow(minutes)*60 if row.randomize else 0
            row.planned_at=int(begin.timestamp())+offset
            row.planned_cycle=result['cycle']
            await self.session.flush()
        result['planned_at']=row.planned_at
        if now.timestamp() < row.planned_at:
            result['status']='waiting'
            await self.session.commit()
            return result
        # Compare-and-set makes two workers and a restart share the same daily claim.
        old_cycle = row.last_cycle
        changed = await self.session.execute(update(ProductPolishSchedule).where(
            ProductPolishSchedule.id == row.id, ProductPolishSchedule.last_cycle == old_cycle,
        ).values(last_cycle=result['cycle'], last_status='claimed'))
        await self.session.commit()
        if changed.rowcount != 1:
            result['status'] = 'already_processed'
        return result
