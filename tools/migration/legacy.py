"""Explicit GuDong compatibility. Never imports or starts its DB manager."""
from copy import deepcopy
import math
import os
import stat
import time
from string import Formatter
from cryptography.fernet import Fernet, InvalidToken
from .snapshot import MigrationError


def load_source_key(*, env=None, key_file=None):
    if env and key_file:
        raise MigrationError('source_key_ambiguous')
    if not env and not key_file:
        return None
    try:
        if env:
            value = os.environ[env].strip().encode()
        else:
            fd = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise MigrationError('source_key_file_not_private')
                value = handle.read(1024).strip()
        Fernet(value)
        return value
    except (OSError, KeyError, ValueError):
        raise MigrationError('source_key_invalid') from None


def decrypt_rows(snapshot, source_key):
    rows = deepcopy(snapshot.tables)
    if source_key is None:
        return rows
    try:
        cipher = Fernet(source_key)
        # Audited GuDong encrypts these fields, not arbitrary text beginning enc$.
        for row in rows['cookies']:
            for field in ('value', 'password', 'proxy_pass'):
                value = row.get(field)
                if isinstance(value, str) and value.startswith('enc$'):
                    row[field] = cipher.decrypt(value[4:].encode()).decode('utf-8')
    except (ValueError, InvalidToken, UnicodeError):
        raise MigrationError('legacy_secret_authentication_failed') from None
    return rows


def runtime_pauses(state, *, namespace, checksum, accounts, now=None):
    if state is None:
        return []
    now = time.time() if now is None else now
    try:
        captured = float(state['captured_at'])
        if not math.isfinite(captured) or not 0 <= now-captured <= 900:
            raise MigrationError('runtime_snapshot_stale')
        if state['namespace'] != namespace or state['source_checksum'] != checksum:
            raise MigrationError('runtime_source_mismatch')
        output, seen = [], set()
        for entry in state['accounts']:
            account_id = entry['account_id']
            if account_id in seen:
                raise MigrationError('runtime_duplicate_account')
            seen.add(account_id)
            if account_id not in accounts or entry['user_id'] != accounts[account_id]['user_id']:
                raise MigrationError('runtime_owner_mismatch')
            for chat, until in entry['paused_chats'].items():
                until = float(until)
                if not isinstance(chat, str) or not chat or not math.isfinite(until):
                    raise MigrationError('runtime_pause_invalid')
                # Expired state is evidence, not an instruction to start a new timer.
                if until > now:
                    output.append({'account_id':account_id,'chat_id':chat,'until':until})
        return output
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MigrationError('runtime_snapshot_invalid') from None


def convert_setting(key, value):
    """Known equivalent target keys only. Return None for an explicit per-key gap."""
    boolean = {'registration_enabled', 'show_default_login_info', 'login_captcha_enabled', 'smtp_use_tls', 'smtp_use_ssl'}
    if key in boolean:
        text = str(value).strip().lower()
        if text not in {'true','false','1','0'}:
            raise MigrationError('setting_boolean_invalid')
        return key, 'true' if text in {'true','1'} else 'false'
    if key == 'smtp_port':
        try:
            n=int(str(value))
            if not 0 <= n <= 65535: raise ValueError()
        except ValueError: raise MigrationError('setting_port_invalid') from None
        return key, str(n)
    if key in {'smtp_server','smtp_user','smtp_password','smtp_from'}:
        return key, str(value)
    if key == 'theme_color':
        # Only blue has an audited target preset equivalent; other palettes need review.
        return ('theme.color_preset','ocean') if value == 'blue' else None
    return None


# Keep each legacy event distinct; all legacy variables have a controlled renderer.
from common.services.notification_template_service import DEFAULTS, VARIABLES
EVENTS = {event: event for event in DEFAULTS}


def convert_template(event, body):
    from common.services.notification_template_service import NotificationTemplateService
    try:
        NotificationTemplateService(None).validate(event, body)
    except ValueError:
        raise MigrationError('notification_template_semantics_required') from None
    return event, body


def specifications(row):
    """Two legacy dimensions become one lossless native equality key on card/order."""
    first=(row.get('spec_name'),row.get('spec_value'))
    second=(row.get('spec_name_2'),row.get('spec_value_2'))
    if not any(second): return dict(spec_name=first[0],spec_value=first[1])
    if not all(first+second): raise MigrationError('incomplete_legacy_specification')
    from .snapshot import canonical
    return dict(spec_name=canonical([first[0],second[0]]),spec_value=canonical([first[1],second[1]]))


def commerce(rows, *, add, consume, uid, token, owner, account, issue, common_times):
    cards={r['id']:r for r in rows.get('cards',[])}
    for r in rows.get('delivery_rules',[]):
        card=cards.get(r['card_id'])
        if card is None: raise MigrationError('missing_card_reference')
        if r.get('user_id',card['user_id'])!=card['user_id']: raise MigrationError('cross_owner_reference')
        if not isinstance(r['keyword'],str) or not 1<=len(r['keyword'])<=255: raise MigrationError('invalid_delivery_keyword')
        count=r.get('delivery_count',1)
        if type(count) is not int or not 1<=count<=1000: raise MigrationError('invalid_delivery_count')
        add('delivery_rules',r,'xy_delivery_rules',dict(id=token('delivery_rule',r['id'])[:36],
            owner_id=owner(card['user_id']),account_id=None,card_id=uid('cards',card['id']),
            keyword=r['keyword'],match_mode='legacy_contains',delivery_count=count,
            enabled=bool(r.get('enabled',True)),description=r.get('description'),priority=0,
            delivery_times=int(r.get('delivery_times') or 0),legacy_id=r['id'],version=1,**common_times(r)))
    consume('delivery_rules','id user_id keyword card_id delivery_count enabled description delivery_times')
    templates={}
    for r in rows.get('comment_templates',[]):
        templates.setdefault(r['cookie_id'],[]).append(r)
        add('comment_templates',r,'xy_product_rate_templates',dict(id=uid('rate_template',r['id']),
            owner_id=owner(account(r['cookie_id'])['user_id']),account_id=r['cookie_id'],name=r['name'],
            content=r['content'],active=bool(r.get('is_active')),**common_times(r)))
    for account_id, entries in templates.items():
        active=[r for r in entries if r.get('is_active')]
        if len(active)>1:
            issue('comment_templates','multiple_active_rating_templates'); continue
        if not active: continue
        r=active[0]
        add('comment_templates',r,'xy_auto_rate_configs',dict(id=uid('rating_config',account_id),account_id=account_id,
            enabled=False,rate_type='text',text_content=r['content'],thanks_enabled=False),lookup=('account_id',))
    consume('comment_templates','id cookie_id name content is_active sort_order')
    orders={(r['cookie_id'],r['order_id']) for r in rows.get('orders',[])}
    for source,target in [('scheduled_rate_logs','xy_scheduled_rate_log'),('scheduled_red_flower_logs','xy_scheduled_red_flower_log')]:
        for r in rows.get(source,[]):
            if not r.get('order_id'):
                issue(source,'account_level_log_target_required'); continue
            if (r['cookie_id'],r['order_id']) not in orders: raise MigrationError('missing_order_reference')
            status=r['status'] if r['status'] in ('success','failed') else 'unknown'
            add(source,r,target,dict(id=uid(source,r['id']),batch_id=token('rating_batch',r['batch_id'])[:36],
                account_id=r['cookie_id'],order_no=r['order_id'],status=status,
                error_message='migration_source_details_archived' if r.get('message') or r.get('raw_response') else None,
                **common_times(r)))
        consume(source,'id batch_id cookie_id order_id item_id buyer_id buyer_nick comment status message raw_response')
    # Import native at-most-once feedback barriers separately from log display.
    for source,kind in [('scheduled_rate_logs','rate'),('scheduled_red_flower_logs','red_flower')]:
        grouped={}
        for r in rows.get(source,[]):
            if not r.get('order_id'): continue
            grouped.setdefault((r['cookie_id'],r['order_id']),[]).append(r)
            state=r['status'] if r['status'] in ('success','failed') else 'unknown'
            add(source,r,'xy_product_operation_evidence',dict(id=uid('feedback_evidence',[source,r['id']]),
                owner_id=owner(account(r['cookie_id'])['user_id']),actor_id=owner(account(r['cookie_id'])['user_id']),
                target='feedback:'+r['cookie_id'],action=kind,
                detail=dict(account_id=r['cookie_id'],order_no=r['order_id'],kind=kind,status=state,
                            reason='migration_history',success=state=='success',unknown=state=='unknown'),**common_times(r)))
        for (account_id,order_no),entries in grouped.items():
            status='success' if any(r['status']=='success' for r in entries) else 'unknown'
            identity=[account_id,order_no,kind]
            add(source,entries[-1],'xy_product_feedback_attempts',dict(id=uid('feedback_attempt',identity),
                owner_id=owner(account(account_id)['user_id']),account_id=account_id,order_no=order_no,
                kind=kind,status=status,retry_at=None),lookup=('owner_id','account_id','kind','order_no'),identity=identity)
    for r in rows.get('scheduled_tasks',[]):
        a=account(r['account_id'])
        if r.get('user_id') is not None and r['user_id']!=a['user_id']: raise MigrationError('cross_owner_reference')
        if r.get('task_type')!='item_polish':
            issue('scheduled_tasks','unsupported_schedule_type',task_hash=token('schedule',r['id']))
            continue
        if sum(task.get('account_id')==r['account_id'] and task.get('task_type')=='item_polish' for task in rows['scheduled_tasks'])!=1:
            issue('scheduled_tasks','duplicate_account_schedule',task_hash=token('schedule',r['id']))
            continue
        values=daily_polish_schedule(r)
        add('scheduled_tasks',r,'xy_product_polish_schedules',dict(id=uid('polish_schedule',r['id']),
            owner_id=owner(a['user_id']),account_id=r['account_id'],**values,**common_times(r)),lookup=('owner_id','account_id'))
    consume('scheduled_tasks','id name task_type account_id enabled interval_hours delay_minutes random_delay_max next_run_at last_run_at last_run_result user_id')


def capture_runtime_state(paused_chats, snapshot, *, namespace, key, now=None):
    """Call with a copy of AutoReplyPauseManager.paused_chats after admission stops."""
    now=time.time() if now is None else now
    accounts={r['id']:r for r in snapshot.tables['cookies']}
    grouped={}
    for identity,until in paused_chats.items():
        if not isinstance(identity,tuple) or len(identity)!=2: raise MigrationError('runtime_pause_invalid')
        account_id,chat_id=identity
        if account_id not in accounts: raise MigrationError('runtime_owner_mismatch')
        entry=grouped.setdefault(account_id,{'account_id':account_id,'user_id':accounts[account_id]['user_id'],'paused_chats':{}})
        entry['paused_chats'][chat_id]=until
    state={'namespace':namespace,'source_checksum':snapshot.checksum(key),'captured_at':now,'accounts':list(grouped.values())}
    runtime_pauses(state,namespace=namespace,checksum=state['source_checksum'],accounts=accounts,now=now)
    return state


def daily_polish_schedule(row):
    """GuDong delay_minutes is an hour, random_delay_max is an inclusive minute."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from common.services.product_polish_schedule import polish_window
    hour, spread=row.get('delay_minutes',0),row.get('random_delay_max',10)
    if type(hour) is not int or not 0<=hour<=23 or type(spread) is not int or not 0<=spread<=59:
        raise MigrationError('invalid_daily_schedule')
    zone=ZoneInfo('Asia/Shanghai')
    start=datetime(2000,1,1,hour,tzinfo=zone)
    values=dict(timezone_name=zone.key,start=start.strftime('%H:%M'),
                end=(start+timedelta(minutes=spread+1)).strftime('%H:%M'),randomize=spread>0)
    def local(value):
        try:
            date=datetime.fromisoformat(str(value).replace('Z','+00:00'))
            return date.astimezone(zone) if date.tzinfo else date.replace(tzinfo=zone)
        except ValueError: raise MigrationError('invalid_schedule_timestamp') from None
    if row.get('next_run_at'):
        date=local(row['next_run_at'])
        window=polish_window(date,**{k:values[k] for k in ('timezone_name','start','end')})
        if window['status']!='ready': raise MigrationError('schedule_due_outside_window')
        values.update(planned_at=int(date.timestamp()),planned_cycle=window['cycle'])
    if row.get('last_run_at'):
        date=local(row['last_run_at'])
        values.update(last_cycle=polish_window(date,**{k:values[k] for k in ('timezone_name','start','end')})['cycle'],
                      last_status='migration_history')
    return values
