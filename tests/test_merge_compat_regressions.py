import ast
import copy
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
import reply_server
from utils.item_publisher import ItemPublisher
from test_upstream_categories import recommendation

class MergeCategoryRegressions(unittest.IsolatedAsyncioTestCase):
    def publisher(self, response):
        pub = ItemPublisher('unb=fixture; _m_h5_tk=fixture_1')
        pub.get_public_channel = AsyncMock(return_value=response)
        return pub

    async def test_incomplete_category_never_reaches_publish_write(self):
        response = recommendation()
        response['data'].pop('categoryPredictResult')
        for v in response['data']['cardList'][0]['cardData']['valuesList']:
            v.pop('catId'); v.pop('tbCatId')
        pub = self.publisher(response)
        pub.prepare_image_for_publish = AsyncMock(return_value={'url':'https://example.test/a.png','width':1,'height':1})
        pub.get_default_location = AsyncMock(return_value={})
        pub._post_mtop = AsyncMock(return_value={'ret':['SUCCESS::调用成功']})
        with self.assertRaisesRegex(ValueError, '分类|类目'):
            await pub.publish_item(title='手机', description='描述', images=[{'url':'https://example.test/a.png'}],current_price=1,original_price=None,delivery_choice='包邮',post_price=0,can_self_pickup=False)
        pub._post_mtop.assert_not_awaited()

    async def test_candidates_without_default_remain_selectable(self):
        response = recommendation(); response['data'].pop('categoryPredictResult')
        for v in response['data']['cardList'][0]['cardData']['valuesList']: v['isClicked'] = '0'
        pub = self.publisher(response)
        result = await pub.recommend_categories('手机','描述',[])
        self.assertIsNone(result['category'])
        self.assertEqual(len(result['candidates']), 2)
        self.assertFalse(any(c['is_selected'] for c in result['candidates']))
        chosen = await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'20'})
        self.assertEqual(chosen['category']['cat_id'], '22')

    async def test_display_match_and_label_share_name_aliases(self):
        for option in ({'valueName':'品牌B'}, {'properties':'brand##品牌##品牌B'}):
            response = recommendation()
            response['data']['cardList'][1]['cardData']['valuesList'] = [dict(option, isClicked='1')]
            pub = self.publisher(response)
            result = await pub.recommend_categories('手机','描述',[])
            self.assertEqual(result['properties'][0]['options'][0]['value_name'], '品牌B')
            result = await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'20','attributes':[{'property_id':'brand','value_name':'品牌B'}]})
            label = next(x for x in pub._build_item_label_list(result['cards']) if x['propertyId']=='brand')
            self.assertEqual(label['valueName'], '品牌B')

    async def test_explicit_clear_removes_defaults_but_omitted_property_keeps_them(self):
        response = recommendation(); response['data']['cardList'][1]['cardData']['isMultiple']='1'
        before = copy.deepcopy(response)
        pub = self.publisher(response)
        result = await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'20','attributes':[{'property_id':'brand','values':[]}]})
        self.assertFalse(any(o['is_selected'] for o in result['properties'][0]['options']))
        self.assertFalse(any(x['propertyId']=='brand' for x in pub._build_item_label_list(result['cards'])))
        default = await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'20'})
        self.assertTrue(any(x['propertyId']=='brand' for x in pub._build_item_label_list(default['cards'])))
        self.assertEqual(response,before)

class ClearPersistence(unittest.TestCase):
    def test_empty_values_survive_material_api(self):
        old=dict(reply_server.app.dependency_overrides)
        reply_server.app.dependency_overrides[reply_server.get_current_user]=lambda:{'user_id':1,'username':'fixture'}
        client=TestClient(reply_server.app)
        try:
            choice={'channel_cat_id':'20','attributes':[{'property_id':'brand','values':[]}]}
            r=client.post('/product-materials',json={'title':'测试','description':'描述','platform_category':choice})
            self.assertEqual(r.status_code,200,r.text)
            mid=r.json()['material']['id']
            try:self.assertEqual(client.get(f'/product-materials/{mid}').json()['material']['platform_category'],choice)
            finally:client.delete(f'/product-materials/{mid}')
        finally:
            reply_server.app.dependency_overrides.clear();reply_server.app.dependency_overrides.update(old)

class VerificationCallSites(unittest.TestCase):
    def test_login_and_import_callbacks_pass_structured_verification_context(self):
        # Execute actual nested callbacks, stubbing delivery and log boundaries only.
        tree=ast.parse(Path('reply_server.py').read_text())
        callbacks=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in ('send_face_verification_notification','send_verification_notification')]
        self.assertEqual(len(callbacks),3)
        for callback in callbacks:
            sent=[]
            env={'account_id':'fixture','session_id':'fixture-session','current_user':{'user_id':1},
                 'verification_url':'https://example.test/verify','verification_type_label':'短信验证','message':'需要确认',
                 'actual_screenshot_path':None,'time':__import__('time'),
                 'log_with_user':lambda *a,**kw:None,'build_face_verify_notification':lambda **kw:'legacy',
                 'dispatch_account_notifications_sync':lambda *a,**kw:sent.append(kw) or True}
            module=ast.Module(body=[callback],type_ignores=[])
            exec(compile(ast.fix_missing_locations(module),'reply_server.py','exec'),env)
            env[callback.name]()
            self.assertEqual(len(sent),1)
            self.assertEqual(sent[0].get('template_context',{}).get('verification_url'),'https://example.test/verify')
            self.assertEqual(sent[0]['template_context']['verification_info'],'短信验证')
