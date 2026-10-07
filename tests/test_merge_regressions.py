import unittest
from unittest.mock import AsyncMock
from utils.item_publisher import ItemPublisher
from utils.publish_categories import describe_categories, build_labels
from test_upstream_categories import recommendation

class MergeRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_category_never_reaches_publish(self):
        response=recommendation();response['data'].pop('categoryPredictResult')
        for value in response['data']['cardList'][0]['cardData']['valuesList']:
            value.pop('catId');value.pop('tbCatId')
        pub=ItemPublisher('unb=fixture; _m_h5_tk=x_1')
        pub.get_public_channel=AsyncMock(return_value=response)
        pub.prepare_image_for_publish=AsyncMock(return_value={'url':'https://example.test/a','width':1,'height':1})
        pub.get_default_location=AsyncMock(return_value={})
        pub._post_mtop=AsyncMock(return_value={'ret':['SUCCESS']})
        with self.assertRaisesRegex(ValueError,'分类|类目'):
            await pub.publish_item(title='测试',description='说明',images=[{'url':'https://example.test/a'}],current_price=10,original_price=None,delivery_choice='包邮',post_price=0,can_self_pickup=False)
        pub._post_mtop.assert_not_called()

    def test_unselected_candidates_remain_available(self):
        response=recommendation();response['data'].pop('categoryPredictResult')
        for value in response['data']['cardList'][0]['cardData']['valuesList']:value['isClicked']='0'
        result=describe_categories(response)
        self.assertIsNone(result['category'])
        self.assertEqual(len(result['candidates']),2)
        self.assertFalse(any(c['is_selected'] for c in result['candidates']))

    def test_value_name_only_roundtrip(self):
        response=recommendation()
        response['data']['cardList'][1]['cardData']['valuesList']=[{'valueName':'品牌B','isClicked':'0'}]
        result=describe_categories(response,{'channel_cat_id':'20','attributes':[{'property_id':'brand','value_name':'品牌B'}]})
        labels=build_labels(result['cards'])
        self.assertEqual(next(v for v in labels if v['propertyId']=='brand')['valueName'],'品牌B')

    def test_explicit_empty_values_clear_platform_defaults(self):
        response=recommendation()
        response['data']['cardList'][1]['cardData']['isMultiple']='1'
        result=describe_categories(response,{'channel_cat_id':'20','attributes':[{'property_id':'brand','values':[]}]})
        self.assertFalse(any(v['is_selected'] for v in result['properties'][0]['options']))
        self.assertFalse(any(v['propertyId']=='brand' for v in build_labels(result['cards'])))

    async def test_manual_verification_callers_preserve_context_in_channel_body(self):
        import ast
        from pathlib import Path
        from unittest.mock import patch
        from utils.notification_dispatcher import dispatch_notifications
        tree=ast.parse(Path('reply_server.py').read_text())
        calls=[]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id!='dispatch_account_notifications_sync':
                continue
            args={arg.arg:arg.value for arg in node.keywords}
            event=args.get('notification_type')
            if isinstance(event,ast.Constant) and event.value in ('face_verify','face_verification'):
                self.assertIn('template_context',args)
                context=eval(compile(ast.Expression(args['template_context']),'<call context>','eval'),{},
                    {'verification_url':'https://example.test/verify','verification_type_label':'短信验证','message':'需要本人确认'})
                calls.append(context)
        self.assertEqual(len(calls),3)
        for context in calls:
            with patch('utils.notification_dispatcher.send_channel_notification',new=AsyncMock(return_value=True)) as send:
                await dispatch_notifications([{'type':'webhook','config':{'account_template':'{{verification_url}} / {{verification_info}} / {{detail}}'}}],
                    'old body',account_id='fixture',notification_type='face_verify',template_context=context)
                self.assertEqual(send.call_args.args[2],'https://example.test/verify / 短信验证 / 需要本人确认')

    def test_material_api_accepts_and_preserves_explicit_clear(self):
        import reply_server
        from fastapi.testclient import TestClient
        old=dict(reply_server.app.dependency_overrides)
        reply_server.app.dependency_overrides[reply_server.get_current_user]=lambda:{'user_id':1,'username':'fixture'}
        client=TestClient(reply_server.app)
        mid=None
        try:
            selection={'channel_cat_id':'20','attributes':[{'property_id':'brand','values':[]}]}
            response=client.post('/product-materials',json={'title':'test','description':'desc','platform_category':selection})
            self.assertEqual(response.status_code,200)
            mid=response.json()['material']['id']
            self.assertEqual(client.get(f'/product-materials/{mid}').json()['material']['platform_category'],selection)
        finally:
            if mid:client.delete(f'/product-materials/{mid}')
            reply_server.app.dependency_overrides.clear();reply_server.app.dependency_overrides.update(old)
