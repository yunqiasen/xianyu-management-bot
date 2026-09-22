"""回复图片资源元数据/文件往返及引用删除检查。"""
from __future__ import annotations
import asyncio
import hashlib
import io
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, insert, delete, inspect
from sqlalchemy.exc import IntegrityError
from common.models.reply_state import reply_images, reply_image_refs
from common.services.reply_state import identity

MAX_BYTES = 10 * 1024 * 1024


def validate_image(data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('图片应为1字节至10MB')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {'JPEG', 'PNG', 'GIF', 'WEBP'}:
                raise ValueError('支持JPEG、PNG、GIF、WEBP图片')
            if image.width * image.height > 25000000:
                raise ValueError('图片像素超过2500万')
            image.verify()
            return image.format.lower(), image.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('图片内容无效') from exc


class ReplyImages:
    def __init__(self, sessions, root=None):
        self.sessions = sessions
        from common.services.media_paths import media_root
        self.root = Path(root) if root else media_root() / 'uploads/replies'

    async def upload(self, owner_id, data):
        fmt, (width, height) = await asyncio.to_thread(validate_image, data)
        digest = hashlib.sha256(data).hexdigest()
        key = identity(owner_id, digest)
        relative = f'{owner_id}/{key}.{fmt}'
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # content-addressed: 同用户相同文件复用，另一个用户拥有独立资源身份。
        await asyncio.to_thread(path.write_bytes, data)
        row = dict(id=key, owner_id=owner_id, sha256=digest, path=relative,
            url='/static/uploads/replies/' + relative, mime='image/' + fmt,
            size=len(data), width=width, height=height)
        async with self.sessions() as session:
            try:
                await session.execute(insert(reply_images).values(**row))
                await session.commit()
            except IntegrityError:
                await session.rollback()
        return row

    async def _owned(self, session, owner_id, image_id, lock=False):
        stmt = select(reply_images).where(reply_images.c.id == image_id, reply_images.c.owner_id == owner_id)
        if lock:
            stmt = stmt.with_for_update()
        row = (await session.execute(stmt)).mappings().first()
        if not row:
            raise ValueError('图片不存在或无权操作')
        return dict(row)

    async def read(self, owner_id, image_id):
        async with self.sessions() as session:
            row = await self._owned(session, owner_id, image_id)
        path = (self.root / row['path']).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError('图片路径异常')
        data = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('图片校验失败')
        return data

    async def references(self, owner_id, image_id):
        async with self.sessions() as session:
            await self._owned(session, owner_id, image_id)
            rows = (await session.execute(select(reply_image_refs).where(reply_image_refs.c.image_id == image_id,
                reply_image_refs.c.owner_id == owner_id))).mappings().all()
            return [dict(r) for r in rows] + await self._external_references(session, owner_id, image_id)

    async def _external_references(self, session, owner_id, image_id):
        """核对既有素材JSON与不可变发布快照；不依赖素材仍处于未删除状态。"""
        from common.models.product_material import ProductMaterial
        from common.models.publish_log import PublishLog
        from common.models.xy_keyword_rule import XYKeywordRule
        from common.models.default_reply import DefaultReply
        from common.models.xy_account import XYAccount
        from common.models.reply_state import reply_events
        image = await self._owned(session, owner_id, image_id)
        urls = {image['url'], image['path']}
        def uses(value):
            if isinstance(value, str): return value in urls
            if isinstance(value, dict): return any(uses(v) for v in value.values())
            if isinstance(value, list): return any(uses(v) for v in value)
            return False
        connection = await session.connection()
        tables = set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
        found = []
        for model, fields, source in (
            (ProductMaterial, ('images', 'videos', 'specifications'), 'material'),
            (PublishLog, ('publish_snapshot',), 'publish_snapshot')):
            if model.__tablename__ not in tables:
                continue
            rows = (await session.execute(select(model.__table__).where(model.user_id == owner_id))).mappings()
            for row in rows:
                if any(uses(row[field]) for field in fields):
                    found.append(dict(image_id=image_id, owner_id=owner_id, source=source, source_id=str(row['id'])))
        if XYAccount.__tablename__ in tables:
            accounts = select(XYAccount.account_id).where(XYAccount.owner_id == owner_id)
            for table, source, field, key in (
                (XYKeywordRule.__table__, 'keyword', 'image_url', 'id'),
                (DefaultReply.__table__, 'default_reply', 'reply_image', 'id'),
                (reply_events, 'history', 'content', 'cursor')):
                if table.name not in tables or field not in table.c:
                    continue
                rows = (await session.execute(select(table).where(table.c.owner_id == owner_id) if source == 'keyword' else select(table).where(table.c.account_id.in_(accounts)))).mappings()
                for row in rows:
                    if uses(row[field]):
                        found.append(dict(image_id=image_id, owner_id=owner_id, source=source, source_id=str(row[key])))
        return found

    async def reference(self, owner_id, image_id, source, source_id):
        async with self.sessions() as session:
            await self._owned(session, owner_id, image_id, lock=True)
            key = identity(image_id, source, source_id)
            try:
                await session.execute(insert(reply_image_refs).values(id=key, image_id=image_id, owner_id=owner_id,
                    source=source, source_id=source_id))
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def unreference(self, owner_id, image_id, source, source_id):
        async with self.sessions() as session:
            await session.execute(delete(reply_image_refs).where(reply_image_refs.c.owner_id == owner_id,
                reply_image_refs.c.id == identity(image_id, source, source_id)))
            await session.commit()

    async def delete(self, owner_id, image_id):
        async with self.sessions() as session:
            row = await self._owned(session, owner_id, image_id, lock=True)
            refs = (await session.execute(select(reply_image_refs.c.id).where(reply_image_refs.c.image_id == image_id).limit(1))).first()
            if refs or await self._external_references(session, owner_id, image_id):
                raise ValueError('图片仍被引用，请先解除规则引用')
            await session.execute(delete(reply_images).where(reply_images.c.id == image_id, reply_images.c.owner_id == owner_id))
            await session.commit()
        # 文件保留给离线孤儿清理：避免同内容并发重新上传时误删新引用。
        # 请求删除只删除元数据；读取入口即失效，迁移仍可核对原始文件。
