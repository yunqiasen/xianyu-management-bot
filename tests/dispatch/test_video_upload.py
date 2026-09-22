"""S2 media endpoint fixtures with real dispatcher, video parsing, budget and ledger."""
import base64
import hashlib
import json
import unittest
import cv2
import numpy as np
import test_flow as fixtures
from common.models.xy_account import XYAccount
from common.services import account_policy


class MediaEndpoints:
    def __init__(self):self.calls=[];self.lose_part=False
    def request(self,method,url,**options):
        peer=self
        class Response:
            status=200
            headers={}
            async def __aenter__(self):
                peer.calls.append((method,url,options))
                if method=='PUT':
                    if peer.lose_part:raise TimeoutError('synthetic connection loss')
                    self.headers={'ETag':hashlib.md5(options['data']).hexdigest()}
                return self
            async def __aexit__(self,*args):pass
            async def json(self,**kwargs):
                if 'bizConfig' in url:return {'data':{'apiInfo':{}}}
                if 'upload.init' in url:return {'ret':['SUCCESS::OK'],'data':{'model':{'uploadId':'upload-fixture','uploadUrlList':['https://fixture.oss-cn-hangzhou.aliyuncs.com/part'],'videoUploadPolicy':{'sliceSize':'2MB'}}}}
                if 'upload.complete' in url:return {'ret':['SUCCESS::OK'],'data':{'model':{'fileId':'file-fixture','ossUrl':'https://fixture.oss-cn-hangzhou.aliyuncs.com/video'}}}
                return {'success':True,'object':{'url':'https://img.alicdn.com/cover.jpg'}}
            async def text(self):return json.dumps(await self.json())
        return Response()
    def get(self,url,**options):return self.request('GET',url,**options)
    def post(self,url,**options):return self.request('POST',url,**options)
    def put(self,url,**options):return self.request('PUT',url,**options)


class VideoUploadTests(unittest.IsolatedAsyncioTestCase):
    request=fixtures.FlowTests.request
    async def asyncSetUp(self):
        await fixtures.FlowTests.asyncSetUp(self)
        self.addAsyncCleanup(fixtures.FlowTests.asyncTearDown,self)
        async with self.sessions() as session:
            account=await session.get(XYAccount,1)
            state=account_policy.snapshot(account);state['config_values']['risk']={'min_interval_seconds':.001}
            account_policy.store(account,state);await session.commit()
        self.peer=MediaEndpoints();self.live.session=self.peer;self.live._get_proxy_url=lambda:None
        path=self.tmp.name+'/tiny.mp4'
        writer=cv2.VideoWriter(path,cv2.VideoWriter_fourcc(*'mp4v'),1,(16,16))
        writer.write(np.zeros((16,16,3),dtype=np.uint8));writer.release()
        from pathlib import Path
        self.content=base64.b64encode(Path(path).read_bytes()).decode()

    async def test_video_upload_all_phases_stay_on_executor_and_are_not_replayed(self):
        request=self.request('upload_video',content=self.content,name='tiny.mp4')
        result=await self.dispatcher.execute(request)
        self.assertEqual(result.status,'confirmed',result)
        video=result.result['video']
        self.assertEqual(video['mediaCloudFileId'],'file-fixture')
        self.assertEqual((video['widthSize'],video['heightSize']),(16,16))
        self.assertEqual([method for method,_,_ in self.peer.calls],['GET','GET','PUT','POST','POST'])
        for _,url,options in self.peer.calls:
            cookie=options.get('headers',{}).get('Cookie',options.get('headers',{}).get('cookie'))
            self.assertEqual(cookie,'unb=101; _m_h5_tk=offline_fixture;' if ('h5api' in url or 'stream-upload' in url) else '')
        repeat=await self.dispatcher.execute(request)
        self.assertEqual(repeat.status,'confirmed');self.assertEqual(len(self.peer.calls),5)

    async def test_lost_video_part_receipt_stays_unknown_without_retry(self):
        self.peer.lose_part=True
        request=self.request('upload_video',content=self.content,name='tiny.mp4')
        result=await self.dispatcher.execute(request)
        self.assertEqual(result.status,'unknown',result)
        self.assertEqual(len([r for r in self.peer.calls if r[0]=='PUT']),1)
        await self.dispatcher.execute(request)
        self.assertEqual(len([r for r in self.peer.calls if r[0]=='PUT']),1)
