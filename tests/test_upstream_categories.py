import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from utils.item_publisher import ItemPublisher
import reply_server
from db_manager import DBManager


def recommendation():
    return {'ret':['SUCCESS::调用成功'],'data':{
        'categoryPredictResult': json.dumps({'data':[{'categoryId':'22','catName':'手机','channelCategoryId':'20','taobaoCategoryId':'200'}]}),
        'cardList':[{'cardData':{'propertyId':'-10000','propertyName':'分类','valuesList':[
            {'catId':'11','catName':'手机','channelCatId':'10','tbCatId':'100','isClicked':'0'},
            {'catId':'22','catName':'手机','channelCatId':'20','tbCatId':'200','isClicked':'1'},
        ]}}, {'cardData':{'propertyId':'brand','propertyName':'品牌','valuesList':[
            {'valueId':'b1','text':'品牌A','isClicked':'0','transportData':{'propertyId':'brand','valueId':'b1','valueName':'品牌A'}},
            {'valueId':'b2','text':'品牌B','isClicked':'1','transportData':{'propertyId':'brand','valueId':'b2','valueName':'品牌B'}},
        ]}}]}}


class CategoryCompatTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_uses_nested_aliases_and_selected_values_not_string_truthiness(self):
        pub=ItemPublisher('unb=fixture; _m_h5_tk=x_1')
        response=recommendation();before=copy.deepcopy(response)
        pub.get_public_channel=AsyncMock(return_value=response)
        pub.prepare_image_for_publish=AsyncMock(return_value={'url':'https://example.test/a.jpg','width':1,'height':1})
        pub.get_default_location=AsyncMock(return_value={})
        pub._post_mtop=AsyncMock(return_value={'ret':['SUCCESS::调用成功'],'data':{'itemId':'123456'}})
        await pub.publish_item(title='手机',description='测试',images=[{'url':'https://example.test/a.jpg'}],current_price=10,original_price=None,delivery_choice='包邮',post_price=0,can_self_pickup=False)
        payload=pub._post_mtop.call_args.kwargs['payload']
        self.assertEqual(payload['itemCatDTO']['catId'],'22')
        labels={str(x['propertyId']):x for x in payload['itemLabelExtList']}
        self.assertEqual(labels['-10000']['channelCateId'],'20')
        self.assertEqual(labels['brand']['valueId'],'b2')
        self.assertEqual(response,before)

    async def test_manual_choice_selects_same_name_by_ids_and_preserves_other_cards(self):
        pub=ItemPublisher('unb=fixture')
        pub.get_public_channel=AsyncMock(return_value=recommendation())
        result=await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'10','tb_cat_id':'100','cat_name':'手机'})
        self.assertEqual(result['category']['channel_cat_id'],'10')
        self.assertEqual(len(result['properties']),1)
        self.assertEqual(len(pub.get_public_channel.call_args.kwargs['selection']['current_card_list']),2)
        with self.assertRaises(ValueError):
            await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'10','tb_cat_id':'200'})

    async def test_unknown_manual_property_stops_before_publish(self):
        pub=ItemPublisher('unb=fixture')
        pub.get_public_channel=AsyncMock(return_value=recommendation())
        with self.assertRaises(ValueError):
            await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'20','attributes':[{'property_id':'brand','value_id':'not-exists'}]})

    async def test_manual_channel_choice_gets_predicted_leaf_id_when_card_omits_it(self):
        response = recommendation()
        for v in response['data']['cardList'][0]['cardData']['valuesList']:
            v.pop('catId')
        pub = ItemPublisher('unb=fixture')
        pub.get_public_channel = AsyncMock(return_value=response)
        result = await pub.recommend_categories('手机','描述',[],platform_category={'channel_cat_id':'20'})
        self.assertEqual(result['category']['cat_id'], '22')

    async def test_failed_platform_response_is_not_reported_as_success(self):
        pub = ItemPublisher('unb=fixture')
        pub.get_public_channel = AsyncMock(return_value={'ret':['FAIL_SYS_USER_VALIDATE::身份验证']})
        with self.assertRaises(ValueError):
            await pub.recommend_categories('手机','描述',[])

    async def test_multiple_property_values_are_preserved_in_labels(self):
        response = recommendation()
        response['data']['cardList'][1]['cardData']['isMultiple'] = '1'
        pub = ItemPublisher('unb=fixture')
        pub.get_public_channel = AsyncMock(return_value=response)
        choice = {'channel_cat_id':'20','attributes':[{'property_id':'brand','values':[{'value_id':'b1'},{'value_id':'b2'}]}]}
        result = await pub.recommend_categories('手机','描述',[],platform_category=choice)
        labels = pub._build_item_label_list(result['cards'])
        self.assertEqual([v['valueId'] for v in labels if v['propertyId']=='brand'], ['b1','b2'])
        self.assertTrue(result['properties'][0]['is_multiple'])
        response['data']['cardList'][1]['cardData']['isMultiple'] = False
        with self.assertRaises(ValueError):
            await pub.recommend_categories('手机','描述',[],platform_category=choice)


class MaterialCategoryPersistence(unittest.TestCase):
    def test_category_survives_create_update_list_and_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            db=DBManager(d+'/test.db')
            selected={'channel_cat_id':'20','cat_name':'手机','attributes':[{'property_id':'brand','value_id':'b2'}]}
            mid=db.add_product_material(1,{'title':'测试','description':'描述','platform_category':selected})
            self.assertIsNotNone(mid)
            self.assertEqual(db.get_product_material(mid,1).get('platform_category'),selected)
            db.conn.close();db=DBManager(d+'/test.db')
            self.assertEqual(db.list_product_materials(1)['list'][0]['platform_category'],selected)
            self.assertEqual(db.list_product_materials_by_ids([mid],1)[0]['platform_category'],selected)
            self.assertIsNone(db.get_product_material(mid,2))
            self.assertTrue(db.update_product_material(mid,1,{'platform_category':None}))
            self.assertIsNone(db.get_product_material(mid,1)['platform_category']);db.conn.close()

class CategoryAPITests(unittest.TestCase):
    def setUp(self):
        self.old=dict(reply_server.app.dependency_overrides)
        reply_server.app.dependency_overrides[reply_server.get_current_user]=lambda:{'user_id':1,'username':'fixture','is_admin':True}
        self.client=TestClient(reply_server.app)
    def tearDown(self):
        reply_server.app.dependency_overrides.clear();reply_server.app.dependency_overrides.update(self.old)
    def test_material_api_saves_category_without_contacting_platform(self):
        data={'title':'test','description':'desc','images':[{'url':'https://example.test/a.jpg'}], 'platform_category':{'channel_cat_id':'20','cat_name':'手机'}}
        r=self.client.post('/product-materials',json=data)
        self.assertEqual(r.status_code,200)
        mid=r.json()['material']['id']
        try:
            self.assertEqual(r.json()['material'].get('platform_category'),data['platform_category'])
            result=self.client.put(f'/product-materials/{mid}',json={'title':'changed'})
            self.assertEqual(result.json()['material']['platform_category'],data['platform_category'])
        finally:self.client.delete(f'/product-materials/{mid}')
    def test_recommendation_endpoint_is_scoped_and_read_only(self):
        r=self.client.post('/product-publish/categories',json={'account_id':'not-mine','title':'手机','description':'描述'})
        self.assertIn(r.status_code,(403,404))
        with patch.object(reply_server,'_ensure_cookie_access',return_value='fixture'), \
             patch.object(reply_server,'_get_user_cookies_map',return_value={'fixture':'unb=fixture'}), \
             patch.object(ItemPublisher,'get_public_channel',new=AsyncMock(return_value=recommendation())), \
             patch.object(ItemPublisher,'publish_item',new=AsyncMock()) as publish:
            r=self.client.post('/product-publish/categories',json={'account_id':'fixture','title':'手机','description':'描述'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(len(r.json()['candidates']),2)
        publish.assert_not_called()
    def test_single_publish_forwards_selected_category(self):
        selection={'channel_cat_id':'20','cat_name':'手机'}
        with patch.object(reply_server,'_publish_product_to_account',new=AsyncMock(return_value={'success':True})) as publish:
            r=self.client.post('/product-publish',json={'account_id':'fixture','title':'手机','description':'描述','images':[{'url':'https://example.test/a.jpg'}],'platform_category':selection})
        self.assertEqual(r.status_code,200)
        self.assertEqual(publish.call_args.kwargs.get('platform_category'),selection)

    def test_multipart_and_batch_forward_saved_category_and_invalid_json_fails(self):
        choice = {'channel_cat_id':'20','attributes':[{'property_id':'brand','value_id':'b2'}]}
        with patch.object(reply_server,'_publish_product_to_account',new=AsyncMock(return_value={'success':True})) as publish:
            response = self.client.post('/item-publish',data={'cookie_id':'fixture','title':'test','description':'desc','delivery_choice':'包邮','platform_category':json.dumps(choice)},files={'images':('a.png',b'fixture','image/png')})
            self.assertEqual(response.status_code,200)
            self.assertEqual(publish.call_args.kwargs['platform_category'],choice)
            response = self.client.post('/item-publish',data={'cookie_id':'fixture','title':'test','delivery_choice':'包邮','platform_category':'{'},files={'images':('a.png',b'fixture','image/png')})
            self.assertEqual(response.status_code,400)
        material = self.client.post('/product-materials',json={'title':'test','description':'desc','images':[{'url':'https://example.test/a.jpg'}],'platform_category':choice}).json()['material']
        try:
            with patch.object(reply_server,'_ensure_cookie_access',return_value='fixture'), patch.object(reply_server,'_publish_product_to_account',new=AsyncMock(return_value={'success':True})) as publish:
                response = self.client.post('/product-publish/batch',json={'account_ids':['fixture'],'material_ids':[material['id']]})
            self.assertEqual(response.status_code,200)
            self.assertEqual(publish.call_args.kwargs['platform_category'],choice)
            reply_server.app.dependency_overrides[reply_server.get_current_user]=lambda:{'user_id':2,'username':'other'}
            self.assertEqual(self.client.get('/product-materials/'+str(material['id'])).status_code,404)
        finally:
            reply_server.app.dependency_overrides[reply_server.get_current_user]=lambda:{'user_id':1,'username':'fixture'}
            self.client.delete('/product-materials/'+str(material['id']))
