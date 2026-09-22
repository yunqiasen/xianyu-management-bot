"""Local media references are bound to one owner, never arbitrary filesystem paths."""
import os
from pathlib import Path

MEDIA_KINDS = frozenset({'products', 'replies', 'cards', 'keywords', 'item_reply',
                        'default_reply', 'confirm_receipt', 'images', 'files'})


def media_root(static_root=None):
    value = static_root or os.environ.get('STATIC_DIR')
    if value and Path(value).is_absolute():
        return Path(value).resolve()
    return Path(__file__).resolve().parents[2] / 'backend-web/static'


def owned_media_path(value, owner_id, *, static_root=None, kinds=MEDIA_KINDS):
    if not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id < 1:
        raise ValueError('media_owner_required')
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('media_reference_invalid')
    normalized = value.strip().replace('\\', '/')
    if '..' in Path(normalized).parts:
        raise ValueError('media_reference_invalid')
    root = media_root(static_root).resolve()
    if normalized.startswith(('/static/', 'static/')):
        path = root / normalized.lstrip('/')[len('static/'):]
    elif Path(normalized).is_absolute():
        path = Path(normalized)
    else:
        raise ValueError('media_reference_invalid')
    try:
        path = path.resolve()
        relative = path.relative_to(root)
    except (ValueError, OSError, RuntimeError) as exc:
        raise ValueError('media_reference_invalid') from exc
    parts = relative.parts
    if len(parts) != 4 or parts[0] != 'uploads' or parts[1] not in kinds or parts[2] != str(owner_id):
        raise ValueError('media_path_unowned')
    return path


def read_owned_media(value, owner_id, *, static_root=None, max_bytes, kinds=MEDIA_KINDS):
    path = owned_media_path(value, owner_id, static_root=static_root, kinds=kinds)
    try:
        with path.open('rb') as stream:
            content = stream.read(max_bytes + 1)
    except OSError as exc:
        raise ValueError('media_file_missing') from exc
    if not content or len(content) > max_bytes:
        raise ValueError('media_size_invalid')
    return content, path.name
