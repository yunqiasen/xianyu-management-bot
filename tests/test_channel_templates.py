import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from fastapi.testclient import TestClient
import reply_server
from utils.notification_dispatcher import dispatch_notifications


class ChannelTemplates(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_each_channel_receives_own_template(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,*a):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}'
            sent=await dispatch_notifications([
                {'type':'webhook','config':{'url':url,'chat_template':'A {{buyer_nick}}: {{message}}'}},
                {'type':'webhook','config':{'url':url,'chat_template':'B {{account_id}}'}},
            ],'old body',account_id='acct',notification_type='message',
               template_context={'buyer_name':'买家','message':'literal {{account_id}}'})
            self.assertTrue(sent)
            self.assertEqual([r['message'] for r in received],['A 买家: literal {{account_id}}','B acct'])
        finally:
            server.shutdown();server.server_close();thread.join()


class TemplateAPITests(unittest.TestCase):
    def setUp(self):
        self.old=dict(reply_server.app.dependency_overrides)
        reply_server.app.dependency_overrides[reply_server.get_current_user]=lambda:{'user_id':1,'username':'fixture','is_admin':True}
        self.client=TestClient(reply_server.app)
    def tearDown(self):
        reply_server.app.dependency_overrides.clear();reply_server.app.dependency_overrides.update(self.old)
    def test_invalid_channel_template_is_rejected_before_saving(self):
        with patch('db_manager.db_manager.create_notification_channel') as write:
            result=self.client.post('/notification-channels',json={'name':'fixture','type':'webhook','config':json.dumps({'chat_template':'{{typo}}'})})
        self.assertEqual(result.status_code,400)
        write.assert_not_called()
    def test_preview_supports_alias_and_validates_unknown_variables(self):
        r=self.client.post('/notification-template-preview',json={'template_type':'chat','template':'{{buyer_nick}} / {{message}}','context':{'buyer_name':'测试买家','message':'{{account_id}}'}})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()['preview'],'测试买家 / {{account_id}}')
        r=self.client.post('/notification-template-preview',json={'template_type':'chat','template':'{{bad}}'})
        self.assertEqual(r.status_code,400)
    def test_empty_template_uses_global_default_and_update_validates(self):
        r=self.client.post('/notification-template-preview',json={'template_type':'chat','template':'','context':{'buyer_nick':'买家'}})
        self.assertEqual(r.status_code,200)
        self.assertIn('买家',r.json()['preview'])
        with patch('db_manager.db_manager.update_notification_channel') as write:
            r=self.client.put('/notification-channels/10',json={'name':'test','config':'{"delivery_template":"{{"}'})
        self.assertEqual(r.status_code,400);write.assert_not_called()
