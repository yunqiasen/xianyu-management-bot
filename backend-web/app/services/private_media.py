"""Browser previews use a read-only cookie; API Bearer authentication stays separate."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from jose import jwt
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from sqlalchemy import select
from app.core.config import get_settings
from app.core.security import decode_token
from common.models.user import User, UserRole, UserStatus
from common.services.media_paths import owned_media_path

MEDIA_COOKIE = 'xymb_media'
COOKIE_PATH = '/static/uploads'


def set_media_cookie(response, request, user):
    settings = get_settings()
    lifetime = max(60, int(settings.access_token_expire_minutes * 60))
    token = jwt.encode({'sub': str(user.id), 'type': 'asset', 'token_version': user.token_version or 0,
                        'exp': datetime.now(timezone.utc) + timedelta(seconds=lifetime)},
                       settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    response.set_cookie(MEDIA_COOKIE, token, max_age=lifetime, path=COOKIE_PATH,
                        httponly=True, samesite='strict', secure=request.url.scheme == 'https')


def clear_media_cookie(response):
    response.delete_cookie(MEDIA_COOKIE, path=COOKIE_PATH, httponly=True, samesite='strict')


class PrivateMediaFiles(StaticFiles):
    async def get_response(self, path, scope):
        if path.split('/', 1)[0] != 'uploads':
            return await super().get_response(path, scope)
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        request = Request(scope)
        auth = request.headers.get('authorization', '')
        token = auth[7:] if auth.startswith('Bearer ') else request.cookies.get(MEDIA_COOKIE, '')
        expected_type = 'access' if auth else 'asset'
        try:
            claims = decode_token(token)
            if claims.get('type') != expected_type or not str(claims.get('sub', '')).isdigit():
                raise ValueError('invalid_asset_session')
        except (ValueError, TypeError):
            return Response(status_code=401, headers=headers)
        from common.db.session import async_session_maker
        async with async_session_maker() as db:
            user = await db.get(User, int(claims['sub']))
            if (not user or user.status != UserStatus.ACTIVE
                    or claims.get('token_version', 0) != (user.token_version or 0)):
                return Response(status_code=401, headers=headers)
            parts = Path(path).parts
            if len(parts) != 4 or not parts[2].isdigit():
                return Response(status_code=404, headers=headers)
            target_owner = int(parts[2]) if user.role == UserRole.ADMIN else user.id
            try:
                owned_media_path('/static/' + path, target_owner, static_root=self.directory)
            except ValueError:
                return Response(status_code=404, headers=headers)
            # Deleting an image's metadata immediately revokes the retained orphan file.
            if parts[1] == 'replies':
                from common.models.reply_state import reply_images
                found = await db.scalar(select(reply_images.c.id).where(
                    reply_images.c.owner_id == target_owner, reply_images.c.url == '/static/' + path))
                if not found:
                    return Response(status_code=404, headers=headers)
        response = await super().get_response(path, scope)
        response.headers.update(headers)
        return response
