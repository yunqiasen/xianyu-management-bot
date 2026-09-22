"""Image bytes cross RPC; only the account executor talks to the fixed upload endpoint."""
import base64
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4
import aiohttp
from pydantic import BaseModel, ConfigDict, Field, model_validator
from common.services.reply_images import MAX_BYTES, validate_image

UPLOAD_URL = 'https://stream-upload.goofish.com/api/upload.api?floderId=0&appkey=xy_chat&_input_charset=utf-8'
PRODUCT_UPLOAD_URL = 'https://stream-upload.goofish.com/api/upload.api?floderId=0&appkey=fleamarket&_input_charset=utf-8'


class ImageUploadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    purpose: Literal['chat', 'product'] = 'chat'
    content: str | None = Field(default=None, min_length=1, max_length=4*((MAX_BYTES+2)//3))
    url: str | None = Field(default=None, max_length=4096)

    @model_validator(mode='after')
    def one_source(self):
        if bool(self.content) == bool(self.url):
            raise ValueError('exactly_one_media_source_required')
        if self.url is not None:
            from common.services.remote_media import validate_media_url
            validate_media_url(self.url)
        return self


async def execute_image_upload(context, payload):
    from common.services.account_dispatch import DispatchError, ImageMessage, PlatformFailure
    try:
        if payload.get('url'):
            from common.services.remote_media import read_remote_media
            content, _ = await read_remote_media(context, payload['url'], max_bytes=MAX_BYTES)
        else:
            content = base64.b64decode(payload['content'], validate=True)
        extension, (width, height) = validate_image(content)
    except (ValueError, TypeError) as exc:
        raise DispatchError('invalid_image') from exc
    session = getattr(context.live, 'session', None)
    if session is None:
        raise DispatchError('executor_http_unavailable')
    account = await context.check()
    data = aiohttp.FormData()
    data.add_field('file', content, filename='image.'+extension,
                  content_type='image/jpeg' if extension=='jpg' else 'image/'+extension)
    await context.before_external()
    endpoint = PRODUCT_UPLOAD_URL if payload.get('purpose') == 'product' else UPLOAD_URL
    async with session.post(endpoint, data=data, headers={'Cookie':account.cookie,
            'Referer':'https://www.goofish.com/'}, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=30)) as response:
        if response.status==429:
            from common.services.account_request_budget import parse_retry_after
            delay = parse_retry_after(response.headers.get('Retry-After')) or 60
            await context.budget.defer(context.request.account_id, delay)
            raise PlatformFailure('platform_rate_limited', retry_after=delay)
        if response.status!=200:
            raise DispatchError('upload_result_unverified')
        result=await response.json(content_type=None)
    await context.check()
    candidates = [result]+[result.get(key) for key in ('data','object','result')] if isinstance(result,dict) else []
    url=next((value.get('url') or value.get('fileUrl') or value.get('file_url')
              for value in candidates if isinstance(value,dict) and (value.get('url') or value.get('fileUrl') or value.get('file_url'))), None)
    if not url:
        raise DispatchError('upload_result_unverified')
    try:
        checked=ImageMessage(cid='image',to_user_id='image',image_url=url,width=width,height=height)
    except ValueError as exc:
        raise DispatchError('upload_url_unverified') from exc
    return {'url':checked.image_url,'width':width,'height':height}


async def upload_account_image(account_id, owner_id, path, *, request_id=None, rpc=None, sessions=None):
    with Path(path).open('rb') as file:
        content=file.read(MAX_BYTES+1)
    result=await upload_account_image_content(account_id,owner_id,content,request_id=request_id,rpc=rpc,sessions=sessions)
    return result['url']


async def upload_account_image_content(account_id, owner_id, content, *, purpose='chat', request_id=None, rpc=None, sessions=None):
    validate_image(content)
    return await _submit_image(account_id, owner_id, {'content':base64.b64encode(content).decode(), 'purpose':purpose},
                               request_id=request_id, rpc=rpc, sessions=sessions)


async def upload_account_image_url(account_id, owner_id, url, *, purpose='product', request_id=None, rpc=None, sessions=None):
    from common.services.remote_media import validate_media_url
    validate_media_url(url)
    return await _submit_image(account_id, owner_id, {'url':url, 'purpose':purpose},
                               request_id=request_id, rpc=rpc, sessions=sessions)


async def _submit_image(account_id, owner_id, payload, *, request_id=None, rpc=None, sessions=None):
    from common.services.account_dispatch import AccountDispatchClient, DispatchError, CURRENT_OPERATION, PlatformFailure
    context=CURRENT_OPERATION.get()
    if context is not None:
        if context.request.account_id != account_id or context.request.owner_id != owner_id:
            raise DispatchError('account_identity_mismatch')
        return await execute_image_upload(context,payload)
    if not account_id or owner_id is None:
        raise DispatchError('account_identity_required')
    if sessions is None:
        from common.db.session import async_session_maker
        sessions=async_session_maker
    if rpc is None:
        from app.core.config import get_settings
        settings=get_settings()
        rpc=AccountDispatchClient(settings.websocket_service_url,settings.internal_api_token)
    rid='image-'+sha256((request_id or uuid4().hex).encode()).hexdigest()
    async with sessions() as session:
        result=await rpc.submit(owner_id=owner_id,account_id=account_id,request_id=rid,
            command='upload_image',payload=payload,session=session)
    if result.status!='confirmed':
        raise PlatformFailure(result.error_code or 'upload_result_unverified', retry_after=result.retry_after)
    return result.result
