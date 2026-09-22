"""S1 rules CRUD -> a later payment -> real stock and observable message payloads."""
from unittest.mock import AsyncMock
from datetime import datetime
from sqlalchemy import select
from common.models.xy_order import XYOrder
from common.models.card import Card
from common.models.xy_catalog_item import XYCatalogItem
from common.db.base_class import Base
from common.services.order_delivery_runtime import OrderDeliveryRuntime
import unittest
import test_api as api_fixtures


class DeliveryRuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await api_fixtures.CommerceApiTests.asyncSetUp(self)
        async with self.engine.begin() as connection:
            await connection.run_sync(XYCatalogItem.__table__.create)
            if 'xy_delivery_rules' in Base.metadata.tables:
                await connection.run_sync(lambda conn: Base.metadata.tables['xy_delivery_rules'].create(conn, checkfirst=True))
        async with self.sessions() as session:
            card = await session.get(Card, 1); card.data_content = 'A\nB\nC\nD\nE\nF'
            order = await session.get(XYOrder, 1)
            order.buyer_id = 'buyer'; order.chat_id = 'chat'; order.item_id = 'future-item'
            order.item_snapshot = {'title': '订阅 软件半年卡'}
            await session.commit()

    async def asyncTearDown(self):
        await api_fixtures.CommerceApiTests.asyncTearDown(self)

    async def test_dynamic_title_rule_applies_to_future_item_with_multiplier_and_cas(self):
        base = '/api/v1/orders/commerce/rules'
        response = await self.client.post(base, json={'card_id':1, 'keyword':'软件', 'delivery_count':2, 'enabled':True})
        self.assertEqual(response.status_code, 200, response.text)
        rule = response.json()['data']
        preview = (await self.client.get('/api/v1/orders/commerce/rule-preview', params={'account_id':'a','order_no':'same'})).json()['data']
        selected = next(row for row in preview if row['matched'])
        self.assertEqual(selected['rule_id'], rule['id']); self.assertEqual(selected['quantity'], 4)
        text = AsyncMock(return_value={'delivery_state':'confirmed'}); confirm = AsyncMock(return_value='confirmed')
        await OrderDeliveryRuntime(self.sessions).handle(account_id='a',order_no='same',item_id='future-item',buyer_id='buyer',chat_id='chat',send_text=text,send_image=text,confirm=confirm,mode='send_first')
        self.assertEqual([call.args[0] for call in text.call_args_list], ['A','B','C','D'])
        async with self.sessions() as session:
            self.assertEqual((await session.get(Card,1)).data_content,'E\nF')
        response=await self.client.put(base+'/'+rule['id'],json={'enabled':False,'expected_version':rule['version']})
        self.assertEqual(response.status_code,200,response.text)
        response=await self.client.put(base+'/'+rule['id'],json={'enabled':True,'expected_version':rule['version']})
        self.assertEqual(response.status_code,409)
        self.user.id=2
        self.assertEqual((await self.client.get(base)).json()['data'],[])
        self.assertEqual((await self.client.put(base+'/'+rule['id'],json={'enabled':True,'expected_version':rule['version']+1})).status_code,404)

    async def prepare_lines(self, blue_stock='B1\nB2'):
        async with self.sessions() as session:
            red = await session.get(Card,1)
            red.item_id='future-item'; red.is_multi_spec=True; red.spec_name='颜色'; red.spec_value='红'; red.data_content='R1\nR2\nR3'
            session.add(Card(id=2,user_id=1,item_id='future-item',name='蓝色卡',type='data',data_content=blue_stock,
                             is_multi_spec=True,spec_name='颜色',spec_value='蓝'))
            order=await session.get(XYOrder,1)
            order.quantity=3; order.metadata_json={'order_lines':[
                {'line_id':'red-line','item_id':'future-item','spec_name':'颜色','spec_value':'红','quantity':2,'amount':'20.00'},
                {'line_id':'blue-line','item_id':'future-item','spec_name':'颜色','spec_value':'蓝','quantity':1,'amount':'11.00'}]}
            await session.commit()

    async def test_one_order_with_two_skus_sends_correct_units_then_confirms_once(self):
        await self.prepare_lines()
        text=AsyncMock(return_value={'delivery_state':'confirmed'}); confirm=AsyncMock(return_value='confirmed')
        for _ in range(2):
            await OrderDeliveryRuntime(self.sessions).handle(account_id='a',order_no='same',item_id='future-item',buyer_id='buyer',chat_id='chat',send_text=text,send_image=text,confirm=confirm,mode='send_first')
        self.assertCountEqual([c.args[0] for c in text.call_args_list], ['R1','R2','B1'])
        self.assertEqual(confirm.call_count,1)
        listed=(await self.client.get('/api/v1/orders/commerce/intents')).json()['data']
        self.assertEqual({r['line_id']:r['quantity'] for r in listed}, {'red-line':2,'blue-line':1})
        self.assertTrue(all(r['content_state']=='confirmed' for r in listed))
        self.assertEqual({r['source']['line_amount'] for r in listed},{'20.00','11.00'})

    async def test_short_stock_in_second_line_rolls_back_batch_before_any_send(self):
        await self.prepare_lines()
        async with self.sessions() as session:
            (await session.get(Card,1)).data_content=''
            await session.commit()
        text=AsyncMock(return_value={'delivery_state':'confirmed'})
        await OrderDeliveryRuntime(self.sessions).handle(account_id='a',order_no='same',item_id='future-item',buyer_id='buyer',chat_id='chat',send_text=text,send_image=text,confirm=text,mode='send_first')
        text.assert_not_called()
        async with self.sessions() as session:
            self.assertEqual((await session.get(Card,2)).data_content,'B1\nB2')
        self.assertEqual((await self.client.get('/api/v1/orders/commerce/intents')).json()['data'],[])

    async def test_legacy_rule_quantity_switch_is_read_from_current_item_config(self):
        async with self.sessions() as session:
            session.add(XYCatalogItem(id=11,owner_id=1,account_pk=1,item_id='future-item',title='软件卡',
                                     metadata_json={'multi_quantity_delivery':False},created_at=datetime(2026,9,23)))
            await session.commit()
        result=await self.client.post('/api/v1/orders/commerce/rules',json={'card_id':1,'keyword':'软件','delivery_count':2,'enabled':True,'match_mode':'legacy_contains'})
        self.assertEqual(result.status_code,200,result.text)
        response=await self.client.get('/api/v1/orders/commerce/rule-preview',params={'account_id':'a','order_no':'same'})
        self.assertEqual(next(row['quantity'] for row in response.json()['data'] if row['matched']),2)
        async with self.sessions() as session:
            item=await session.get(XYCatalogItem,11);item.metadata_json={'multi_quantity_delivery':True};await session.commit()
        response=await self.client.get('/api/v1/orders/commerce/rule-preview',params={'account_id':'a','order_no':'same'})
        self.assertEqual(next(row['quantity'] for row in response.json()['data'] if row['matched']),4)

    async def test_unknown_first_line_remains_unknown_after_second_line_confirms_and_restart(self):
        await self.prepare_lines()
        async def send(value):
            return {'delivery_state':'unknown' if value=='B1' else 'confirmed'}
        text=AsyncMock(side_effect=send); confirm=AsyncMock(return_value='confirmed')
        for _ in range(2):
            await OrderDeliveryRuntime(self.sessions).handle(account_id='a',order_no='same',item_id='future-item',buyer_id='buyer',chat_id='chat',send_text=text,send_image=text,confirm=confirm,mode='send_first')
        self.assertCountEqual([c.args[0] for c in text.call_args_list],['B1','R1','R2'])
        confirm.assert_not_called()
        async with self.sessions() as session:
            order=await session.get(XYOrder,1)
            self.assertEqual(order.metadata_json['fulfillment']['content_state'],'unknown')
            self.assertFalse(order.delivery_content)
            self.assertEqual((await session.get(Card,2)).data_content,'B2')
        rows=(await self.client.get('/api/v1/orders/commerce/intents')).json()['data']
        self.assertEqual({r['line_id']:r['content_state'] for r in rows},{'blue-line':'unknown','red-line':'confirmed'})

    async def test_confirm_first_confirms_once_before_all_line_contents(self):
        await self.prepare_lines()
        events=[]
        async def confirm(order_no):
            self.assertEqual(order_no,'same'); events.append('confirm'); return 'confirmed'
        async def send(value): events.append(value); return {'delivery_state':'confirmed'}
        for _ in range(2):
            await OrderDeliveryRuntime(self.sessions).handle(account_id='a',order_no='same',item_id='future-item',buyer_id='buyer',chat_id='chat',send_text=send,send_image=send,confirm=confirm,mode='confirm_first')
        self.assertEqual(events,['confirm','B1','R1','R2'])

    async def test_legacy_title_preview_and_send_match_independent_sqlite_like_order(self):
        import sqlite3
        from sqlalchemy import delete
        from common.models.delivery_rule import DeliveryRule
        examples = [
            ('office PRO', ['OFFICE', 'Pro']),
            ('套餐A\nB', ['套餐_%', '套餐A_B']),
            ('xaZby', ['a_b', 'a%b']),
            ('App', ['Appx', 'Appxy']),
            ('%', ['x']),  # A reverse LIKE match with a legitimate zero score.
            ('A_服务', ['fooA1服务bar', '服务']),
            ('é', ['É']),  # SQLite LIKE only folds ASCII, not all Unicode.
            ('ß', ['SS']),
            (r'a\b', [r'a\b']),
        ]
        async with self.sessions() as session:
            (await session.get(Card,1)).data_content='\n'.join('UNIT-'+str(i) for i in range(20))
            await session.commit()
        for index,(title,keywords) in enumerate(examples):
            with self.subTest(title=title):
                async with self.sessions() as session:
                    await session.execute(delete(DeliveryRule))
                    session.add(XYOrder(id=100+index,owner_id=1,account_id='a',order_no='like-'+str(index),
                        item_id='future-item',buyer_id='buyer',chat_id='chat',status='pending_ship',
                        quantity=1,item_snapshot={'title':title}))
                    await session.commit()
                identities={}
                for legacy_id,keyword in enumerate(keywords,1):
                    created=await self.client.post('/api/v1/orders/commerce/rules',json={
                        'card_id':1,'keyword':keyword,'enabled':True,'match_mode':'legacy_contains'})
                    self.assertEqual(created.status_code,200,created.text)
                    identities[legacy_id]=created.json()['data']['id']
                async with self.sessions() as session:
                    for legacy_id,identity in identities.items():
                        (await session.get(DeliveryRule,identity)).legacy_id=legacy_id
                    await session.commit()
                # The source SQL, independent of the new match implementation.
                with sqlite3.connect(':memory:') as source:
                    source.execute('CREATE TABLE rules(id INTEGER PRIMARY KEY,keyword TEXT)')
                    source.executemany('INSERT INTO rules VALUES(?,?)',enumerate(keywords,1))
                    expected=[row[0] for row in source.execute(
                        "SELECT id FROM rules WHERE (? LIKE '%' || keyword || '%' OR keyword LIKE '%' || ? || '%') "
                        "ORDER BY CASE WHEN ? LIKE '%' || keyword || '%' THEN LENGTH(keyword) ELSE LENGTH(keyword)/2 END DESC,id ASC",
                        (title,title,title))]
                order_no='like-'+str(index)
                response=await self.client.get('/api/v1/orders/commerce/rule-preview',params={'account_id':'a','order_no':order_no})
                self.assertEqual(response.status_code,200,response.text)
                rows=response.json()['data']
                self.assertEqual([row['rule_id'] for row in rows],[identities[number] for number in expected])
                send=AsyncMock(return_value={'delivery_state':'confirmed'})
                confirm=AsyncMock(return_value='confirmed')
                await OrderDeliveryRuntime(self.sessions).handle(account_id='a',order_no=order_no,
                    item_id='future-item',buyer_id='buyer',chat_id='chat',send_text=send,send_image=send,
                    confirm=confirm,mode='send_first')
                self.assertEqual(send.call_count,1 if expected else 0)
                if expected:
                    intents=(await self.client.get('/api/v1/orders/commerce/intents')).json()['data']
                    intent=next(row for row in intents if row['order_no']==order_no)
                    self.assertEqual(intent['source']['rule_id'],identities[expected[0]])
                    self.assertEqual(intent['content_state'],'confirmed')
