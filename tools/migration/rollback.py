"""Consume encrypted rollback facts into an explicit offline GuDong SQLite copy.

Three-way reconciliation compares the exported baseline, present old rows, and
new target facts. Never starts an executor or synthesizes per-unit shipment proof.
"""
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from .snapshot import MigrationError, canonical, digest, ident

class RollbackImporter:
    def __init__(self, store, path, *, account_id, namespace):
        self.store=store; self.path=Path(path); self.account_id=account_id; self.namespace=namespace

    def apply(self, bundle, *, execute=False):
        payload=self.store.read_bundle(bundle)
        if payload.get('account')!=self.account_id or payload.get('namespace')!=self.namespace:
            raise MigrationError('rollback_source_mismatch')
        receipt_name='rollback-receipt-'+digest([str(self.path.resolve()),self.namespace,self.account_id],self.store.key)+'.enc'
        receipt=self.store.read_bundle(receipt_name) if (self.store.root/receipt_name).exists() else {'rows':{},'bundles':[]}
        if (bundle in receipt['bundles'] and receipt.get('latest')!=bundle) or payload.get('exported_at_ns',0)<receipt.get('exported_at_ns',0):
            raise MigrationError('rollback_stale_package')
        baseline=payload.get('source_tables')
        if not isinstance(baseline,dict): raise MigrationError('rollback_baseline_required')
        old_account=next((r for r in baseline.get('cookies',[]) if r['id']==self.account_id),None)
        if old_account is None: raise MigrationError('rollback_source_mismatch')
        source_owner=old_account['user_id']
        def uid(table, identity): return 1+int(digest([self.namespace,table,identity],self.store.key)[:8],16)%2_000_000_000
        owner=uid('users',source_owner)
        target=payload['tables']
        for table,rows in target.items():
            for row in rows:
                for column in ('account_id','cookie_id','account_identifier','account_pk'):
                    if column in row and row[column] not in (None,''):
                        expected=uid('cookies',self.account_id) if isinstance(row[column],int) else self.account_id
                        if row[column]!=expected: raise MigrationError('rollback_account_mismatch')
                for column in ('owner_id','user_id'):
                    # AI history user_id is the platform buyer, not a backend identity.
                    inherited_template=table=='xy_notify_templates' and row.get(column)==0
                    legacy_scoped_filter=table=='xy_reply_filters' and row.get(column) is None and row.get('account_id')==self.account_id
                    if column in row and table!='xy_ai_chat_messages' and row[column]!=owner and not (inherited_template or legacy_scoped_filter):
                        raise MigrationError('rollback_owner_mismatch')
        if not self.path.is_file() or self.path.is_symlink(): raise MigrationError('rollback_stage_required')
        connection=None
        try:
            connection=sqlite3.connect(self.path); connection.row_factory=sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            if connection.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise MigrationError('rollback_stage_integrity')
            current_account=connection.execute('SELECT user_id FROM cookies WHERE id=?',(self.account_id,)).fetchone()
            if current_account is None or current_account['user_id']!=source_owner: raise MigrationError('rollback_owner_mismatch')
            # Pause persists even when reconciliation later finds a conflicting fact.
            if execute:
                connection.execute('INSERT INTO cookie_status(cookie_id,enabled) VALUES(?,0) ON CONFLICT(cookie_id) DO UPDATE SET enabled=0',(self.account_id,))
                connection.commit()
            connection.execute('BEGIN IMMEDIATE' if execute else 'BEGIN')
            available={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            operations=[]; conflicts=[]; gaps=[]; unchanged=0; reconciled=dict(receipt['rows'])
            def project(table, lookup, desired, previous=None):
                nonlocal unchanged
                if table not in available:
                    gaps.append({'table':table,'code':'rollback_table_missing'}); return
                columns={r['name'] for r in connection.execute('PRAGMA table_info('+ident(table)+')')}
                missing=set(desired)-columns
                if missing: gaps.append({'table':table,'code':'rollback_columns_missing','fields':sorted(missing)})
                desired={k:v for k,v in desired.items() if k in columns}
                if not set(lookup)<=columns: raise MigrationError('rollback_identity_missing')
                predicate=' AND '.join(ident(k)+' IS ?' for k in lookup)
                found=connection.execute('SELECT * FROM '+ident(table)+' WHERE '+predicate,tuple(lookup.values())).fetchall()
                if len(found)>1: raise MigrationError('rollback_duplicate_identity')
                current=dict(found[0]) if found else None
                row_key=digest([table,lookup],self.store.key)
                if row_key in receipt['rows']: previous=receipt['rows'][row_key]
                reconciled[row_key]=desired
                if current and all(current[k]==v for k,v in desired.items()): unchanged+=1; return
                if current is not None and (previous is None or any(current[k]!=previous.get(k) for k in desired)):
                    conflicts.append({'table':table,'identity_hash':digest(lookup,self.store.key),'code':'rollback_target_changed'}); return
                if current is None and previous is not None:
                    conflicts.append({'table':table,'identity_hash':digest(lookup,self.store.key),'code':'rollback_target_deleted'}); return
                if current is None:
                    # Missing required legacy columns are reported rather than failing halfway.
                    info=connection.execute('PRAGMA table_info('+ident(table)+')').fetchall()
                    if any(c['notnull'] and c['dflt_value'] is None and not c['pk'] and c['name'] not in desired for c in info):
                        gaps.append({'table':table,'code':'rollback_required_column_missing'}); return
                operations.append((table,lookup,desired,current is not None))
            old_orders={(r['cookie_id'],r['order_id']):r for r in baseline.get('orders',[])}
            for row in target.get('xy_orders',[]):
                previous=old_orders.get((self.account_id,row['order_no']))
                fields={'item_id':'item_id','buyer_id':'buyer_id','buyer_nick':'buyer_nick','sid':'chat_id',
                        'quantity':'quantity','amount':'amount','is_rated':'is_rated','is_red_flower':'is_red_flower'}
                desired={'order_id':row['order_no'],'cookie_id':self.account_id}
                for old,new in fields.items():
                    if new in row: desired[old]=str(row[new]) if old in {'quantity','amount'} and row[new] is not None else row[new]
                status=row['status']
                desired['order_status']=status if status in {'completed','cancelled','refunding','shipped'} else 'unknown'
                if previous:
                    # Numeric SQL normalization is not a changed business fact.
                    if 'amount' in desired and previous.get('amount') is not None:
                        from decimal import Decimal
                        if Decimal(str(desired['amount']))==Decimal(str(previous['amount'])): desired['amount']=previous['amount']
                    # Preserve old unknown/pending spelling when target remains quarantined.
                    if status=='pending_verification': desired['order_status']=previous['order_status']
                project('orders',{'order_id':row['order_no']},desired,previous)
            for row in target.get('xy_reply_events',[]):
                if row['event_id'].startswith(('migration:','migration-ai:')): continue
                event_identity=[self.account_id,row['chat_id'],row['event_id']]
                pk=-uid('rollback_message',event_identity)
                desired=dict(id=pk,cookie_id=self.account_id,chat_id=row['chat_id'],sender_id=row['sender_id'],
                    sender_name=row.get('sender_name',''),content=row['content'],direction=1 if row['role']=='assistant' else 2,
                    content_type=2 if row['content_type']=='image' else 1,reply_source='manual' if row['origin']=='manual' else 'platform',
                    created_at=datetime.fromtimestamp(row['occurred_at'],timezone.utc).replace(tzinfo=None).isoformat(' '))
                project('chat_messages',{'id':pk},desired)
            card_baseline={uid('cards',r['id']):r for r in baseline.get('cards',[]) if r['user_id']==source_owner}
            for row in target.get('xy_cards',[]):
                previous=card_baseline.get(row['id'])
                if previous is None:
                    gaps.append({'table':'cards','code':'new_card_identity_reconciliation'}); continue
                # Pools remain disabled: no content can return to automatic dispatch.
                desired={'id':previous['id'],'user_id':source_owner,'enabled':0}
                for field in ('data_content','text_content','api_config','image_url'):
                    if field in previous: desired[field]=row.get(field)
                project('cards',{'id':previous['id']},desired,previous)
            # New target data-card reservations preserve occupancy only. Their ordered
            # reserve list is allocation evidence, never a sent/finalized receipt.
            old_reservations=baseline.get('data_card_reservations',[])
            by_order={}
            for intent in target.get('xy_delivery_intents',[]):
                by_order.setdefault(intent['order_no'],[]).append(intent)
            multiline_orders={row['order_no'] for row in target.get('xy_orders',[])
                if len((row.get('metadata_json') or {}).get('order_lines') or [])>1}
            ambiguous_orders=multiline_orders|{number for number,intents in by_order.items() if len(intents)>1}
            reported_orders=set()
            for intent in target.get('xy_delivery_intents',[]):
                card=card_baseline.get(intent['card_id'])
                lines=intent.get('reserved_lines') or []
                existing=[r for r in old_reservations if r.get('cookie_id')==self.account_id and r['order_id']==intent['order_no']]
                if not card or intent.get('card_type')!='data' or not lines:
                    continue
                if intent['order_no'] in ambiguous_orders:
                    # Old unit_index is order-wide: line-local or redelivery lists do
                    # not prove that identity. Keep the encrypted facts, not guessed rows.
                    if intent['order_no'] not in reported_orders:
                        gaps.append({'table':'data_card_reservations',
                            'identity_hash':digest(intent['order_no'],self.store.key),
                            'code':'multiline_reservation_reconciliation_required'})
                        reported_orders.add(intent['order_no'])
                    continue
                if existing:
                    # Missing units in a legacy partial reservation have no inferred indexes.
                    if any(line not in [r['reserved_content'] for r in existing] for line in lines):
                        gaps.append({'table':'data_card_reservations','code':'partial_reservation_unit_reconciliation'})
                    continue
                if len(lines)!=intent['quantity'] or len(set(lines))!=len(lines):
                    gaps.append({'table':'data_card_reservations','code':'reservation_quantity_reconciliation'}); continue
                free_card=next((r for r in target.get('xy_cards',[]) if r['id']==intent['card_id']),None)
                if free_card and set(lines)&set((free_card.get('data_content') or '').splitlines()):
                    raise MigrationError('reserved_card_in_free_pool')
                for unit,line in enumerate(lines,1):
                    pk=-uid('rollback_reservation',[intent['id'],unit])
                    desired=dict(id=pk,card_id=card['id'],cookie_id=self.account_id,order_id=intent['order_no'],
                                 unit_index=unit,reserved_content=line,status='reserved')
                    project('data_card_reservations',{'id':pk},desired)
            # Target aggregate lines without unit/source provenance remain exported evidence.
            for row in target.get('xy_delivery_intents',[]):
                if row.get('reserved_lines') or row.get('content_state')=='unknown' or row.get('confirm_state')=='unknown':
                    gaps.append({'table':'delivery_intents','identity_hash':digest(row['id'],self.store.key),'code':'unit_evidence_reconciliation_required'})
            consumed={'xy_accounts','xy_users','xy_orders','xy_reply_events','xy_cards','xy_delivery_intents'}
            for table,rows in target.items():
                if rows and table not in consumed:
                    gaps.append({'table':table,'code':'additional_facts_retained_in_bundle','count':len(rows)})
            written=0
            if execute and not conflicts:
                for table,lookup,desired,exists in operations:
                    if exists:
                        sql='UPDATE '+ident(table)+' SET '+','.join(ident(k)+'=?' for k in desired)+' WHERE '+' AND '.join(ident(k)+' IS ?' for k in lookup)
                        connection.execute(sql,tuple(desired.values())+tuple(lookup.values()))
                    else:
                        connection.execute('INSERT INTO '+ident(table)+' ('+','.join(ident(k) for k in desired)+') VALUES ('+','.join('?' for _ in desired)+')',tuple(desired.values()))
                    written+=1
                connection.commit()
            else: connection.rollback()
            report={'written':written,'planned':len(operations),'unchanged':unchanged,'conflicts':conflicts,'gaps':gaps,
                    'account_disabled':execute,'dryrun':not execute,'reconciliation_required':bool(conflicts or gaps)}
            if execute and not conflicts:
                self.store.write_bundle(receipt_name,{'rows':reconciled,'bundles':list(dict.fromkeys(receipt['bundles']+[bundle])),
                    'latest':bundle,'exported_at_ns':payload.get('exported_at_ns',0)})
            if execute:
                self.store.write_bundle('rollback-reconcile-'+digest([bundle,self.account_id],self.store.key)+'.enc',report)
            return report
        except sqlite3.Error:
            raise MigrationError('rollback_database_error') from None
        finally:
            if connection is not None: connection.close()
