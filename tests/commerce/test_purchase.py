import unittest
from aiohttp import web
from common.services.card_purchase import purchase_card
class PurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_supplier_reuses_identity_and_quantity(self):
        calls=[]
        async def receive(request):
            calls.append((dict(request.headers),await request.json()))
            return web.json_response({'data':'A\nB'})
        app=web.Application();app.router.add_post('/buy',receive)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            result=await purchase_card({'url':f'http://127.0.0.1:{port}/buy','method':'POST','response_field':'data','params':{'quantity':'{quantity}'},'idempotency_header':'Idempotency-Key'},quantity=2,idempotency_key='intent-fixture')
            self.assertEqual(result,{'texts':['A','B'],'images':[]})
            self.assertEqual(calls[0][0]['Idempotency-Key'],'intent-fixture');self.assertEqual(len(calls),1)
        finally: await runner.cleanup()
