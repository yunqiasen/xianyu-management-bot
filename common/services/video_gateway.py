"""Video bytes or a public media URL use one durable worker operation."""
import base64
from pathlib import Path
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from common.utils.local_video_upload import DEFAULT_VIDEO_MAX_SIZE, SAFE_VIDEO_EXTS

MAX_VIDEO_BYTES = DEFAULT_VIDEO_MAX_SIZE


class VideoUploadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    content: str | None = Field(default=None, min_length=1, max_length=4*((MAX_VIDEO_BYTES+2)//3))
    url: str | None = Field(default=None, max_length=4096)
    name: str = Field(min_length=1, max_length=160)

    @field_validator('name')
    @classmethod
    def basename(cls, value):
        if Path(value).name != value or '\\' in value or Path(value).suffix.lower() not in SAFE_VIDEO_EXTS:
            raise ValueError('invalid_video_name')
        return value

    @model_validator(mode='after')
    def one_source(self):
        if bool(self.content) == bool(self.url):
            raise ValueError('exactly_one_media_source_required')
        if self.url is not None:
            from common.services.remote_media import validate_media_url
            validate_media_url(self.url)
        return self


async def execute_video_upload(context, payload):
    from common.services.account_dispatch import DispatchError
    from common.services.xianyu_publish_video import _upload_video_content
    try:
        if payload.get('url'):
            from common.services.remote_media import read_remote_media
            content, _ = await read_remote_media(context, payload['url'], max_bytes=MAX_VIDEO_BYTES)
        else:
            content = base64.b64decode(payload['content'], validate=True)
    except (ValueError, TypeError) as exc:
        raise DispatchError('invalid_video') from exc
    if not content or len(content) > MAX_VIDEO_BYTES:
        raise DispatchError('invalid_video_size')
    account = await context.check()
    video, _ = await _upload_video_content({}, content, payload['name'], 'video/mp4', account.cookie,
                                           account.account_id, account.owner_id)
    return {'video': video}


async def upload_account_video(account_id, owner_id, content, name):
    from common.services.account_dispatch import DispatchError
    if not content or len(content) > MAX_VIDEO_BYTES:
        raise DispatchError('invalid_video_size')
    return await _submit_video(account_id, owner_id, {'content':base64.b64encode(content).decode(), 'name':name})


async def upload_account_video_url(account_id, owner_id, url, name):
    from common.services.remote_media import validate_media_url
    validate_media_url(url)
    return await _submit_video(account_id, owner_id, {'url':url, 'name':name})


async def _submit_video(account_id, owner_id, payload):
    from common.services.account_dispatch import AccountDispatchClient, DispatchError, CURRENT_OPERATION, PlatformFailure
    from common.db.session import async_session_maker
    from app.core.config import get_settings
    if not account_id or owner_id is None:
        raise DispatchError('account_identity_required')
    payload = VideoUploadRequest.model_validate(payload).model_dump()
    context = CURRENT_OPERATION.get()
    if context is not None:
        if (context.request.account_id, context.request.owner_id) != (account_id, owner_id):
            raise DispatchError('account_identity_mismatch')
        return (await execute_video_upload(context, payload))['video']
    settings = get_settings()
    rpc = AccountDispatchClient(settings.websocket_service_url, settings.internal_api_token, timeout=915)
    async with async_session_maker() as session:
        result = await rpc.submit(owner_id=owner_id, account_id=account_id, request_id='video-'+uuid4().hex,
            command='upload_video', payload=payload, session=session)
    if result.status != 'confirmed':
        raise PlatformFailure(result.error_code or 'video_result_unverified', retry_after=result.retry_after)
    return result.result['video']
