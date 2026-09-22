"""Publish media is read from an owned local file or fetched by the account executor."""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Any


class PublishMediaError(RuntimeError):
    """A media phase failed; no raw credentials or network exception text."""


def _publish_image(uploaded):
    return {"extraInfo":{"isH":"false","isT":"false","raw":"false"}, "isQrCode":False,
            "url":uploaded['url'], "heightSize":uploaded['height'], "widthSize":uploaded['width'],
            "major":False, "type":0, "status":"done"}


async def upload_publish_image(value: str, cookie: str, *, static_root: str | Path | None = None,
                               account_id: str | None = None, owner_id: int | None = None) -> dict[str, Any]:
    from common.services.image_gateway import upload_account_image_url
    from common.services.media_paths import read_owned_media
    from common.services.reply_images import MAX_BYTES
    from common.services.account_dispatch import DispatchError
    try:
        if value.strip().lower().startswith(('http://', 'https://')):
            return _publish_image(await upload_account_image_url(account_id, owner_id, value.strip()))
        content, name = await asyncio.to_thread(read_owned_media, value, owner_id,
                                                static_root=static_root, max_bytes=MAX_BYTES)
    except (DispatchError, ValueError) as exc:
        raise PublishMediaError(getattr(exc, 'code', 'invalid_image_reference')) from exc
    return await upload_publish_image_content(content, name, cookie, source=value,
                                              account_id=account_id, owner_id=owner_id)


async def upload_publish_image_content(content: bytes, name: str, cookie: str, *, content_type: str | None = None,
                                       source: str = '内存图片', account_id: str | None = None,
                                       owner_id: int | None = None) -> dict[str, Any]:
    """Legacy Cookie argument is retained; only current executor credentials are used."""
    from common.services.image_gateway import upload_account_image_content
    from common.services.account_dispatch import DispatchError
    try:
        uploaded = await upload_account_image_content(account_id, owner_id, content, purpose='product')
    except (DispatchError, ValueError) as exc:
        raise PublishMediaError(getattr(exc, 'code', 'invalid_image')) from exc
    return _publish_image(uploaded)


__all__ = ['PublishMediaError', 'upload_publish_image', 'upload_publish_image_content']
