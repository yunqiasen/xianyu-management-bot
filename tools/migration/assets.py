"""Offline owner-scoped restoration using the native ReplyImages format."""
import copy
import hashlib
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit, unquote
from .snapshot import MigrationError, digest, canonical
from .mapping import Step

FIELDS = {'xy_keyword_rules':('image_url',), 'xy_cards':('image_url','image_urls'),
          'xy_product_materials':('images','specifications'), 'xy_exclusive_replies':('image_url',), 'xy_reply_events':('content',)}


def _strings(value):
    if isinstance(value,str): yield value
    elif isinstance(value,list):
        for item in value: yield from _strings(item)
    elif isinstance(value,dict):
        for name,item in value.items():
            if name in ('image','url','image_url','src','values'): yield from _strings(item)


def _replace(value, old, new):
    if isinstance(value,str): return new if value==old else value
    if isinstance(value,list): return [_replace(x,old,new) for x in value]
    if isinstance(value,dict): return {k:_replace(v,old,new) if k in ('image','url','image_url','src','values') else v for k,v in value.items()}
    return value


def _read_local(root, reference, max_bytes):
    parsed=urlsplit(reference)
    relative=Path(unquote(parsed.path))
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise MigrationError('attachment_requires_local_reference')
    # Walk each directory with O_NOFOLLOW, not only a resolve()+open race.
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            next_fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=next_fd
        file_fd=os.open(relative.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
        with os.fdopen(file_fd,'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode): raise MigrationError('attachment_not_regular')
            data=handle.read(max_bytes+1)
        if len(data)>max_bytes: raise MigrationError('attachment_size_limit')
        return data
    except OSError:
        raise MigrationError('attachment_read_error') from None
    finally: os.close(fd)


def relocate(store, prepared, manifest, *, source_root, target_root):
    from common.services.reply_images import validate_image
    from common.services.reply_state import identity
    result=copy.deepcopy(prepared)
    owners={r['id']:1+int(digest([prepared.namespace,'users',r['id']],store.key)[:8],16)%2_000_000_000 for r in prepared.snapshot.tables['users']}
    pending=[]; seen=set(); extra=[]; resolved=set()
    for entry in manifest:
        try: reference=entry['reference']; source_owner=entry['source_owner']; expected=entry['sha256']
        except (KeyError,TypeError): raise MigrationError('attachment_manifest_invalid') from None
        if (source_owner,reference) in seen: raise MigrationError('attachment_manifest_duplicate')
        seen.add((source_owner,reference))
        owner=owners.get(source_owner)
        matches=[]
        for step in result.steps:
            for field in FIELDS.get(step.target,()):
                if step.target=='xy_reply_events' and step.values['content_type']!='image': continue
                if reference in _strings(step.values.get(field)):
                    step_owner=step.values.get('owner_id',step.values.get('user_id'))
                    if step.target=='xy_reply_events':
                        source_account=next((a for a in prepared.snapshot.tables['cookies'] if a['id']==step.values['account_id']),None)
                        step_owner=owners.get(source_account['user_id']) if source_account else None
                    if step_owner == owner:
                        matches.append((step,field))
        if owner is None or not matches: raise MigrationError('attachment_owner_mismatch')
        data=_read_local(source_root,entry.get('local_path',reference),10*1024*1024)
        sha=hashlib.sha256(data).hexdigest()
        if sha!=expected: raise MigrationError('attachment_hash_mismatch')
        try: fmt,(width,height)=validate_image(data)
        except ValueError: raise MigrationError('attachment_image_invalid') from None
        image_id=identity(owner,sha); relative=f'{owner}/{image_id}.{fmt}'
        url='/static/uploads/replies/'+relative
        image=dict(id=image_id,owner_id=owner,sha256=sha,path=relative,url=url,mime='image/'+fmt,size=len(data),width=width,height=height)
        extra.append(Step('attachments',digest(['asset',owner,sha],store.key),'xy_reply_images',image,digest(entry,store.key),('id',)))
        for step,field in matches:
            step.values[field]=_replace(step.values[field],reference,url)
            source_id=str(step.values['id']) if 'id' in step.values else step.values['event_id']
            ref=dict(id=identity(image_id,step.target,source_id),image_id=image_id,owner_id=owner,source=step.target,source_id=source_id)
            extra.append(Step('attachments',digest(['asset_ref',ref['id']],store.key),'xy_reply_image_refs',ref,digest(entry,store.key),('id',)))
            resolved.add((step.source,reference))
        pending.append((relative,data,sha))
    # All hashes/owners/images checked before any destination directory is created.
    root=Path(target_root)
    if root.is_symlink(): raise MigrationError('attachment_destination_symlink')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for relative,data,sha in pending:
            owner,filename=relative.split('/')
            try: os.mkdir(owner,mode=0o700,dir_fd=root_fd)
            except FileExistsError: pass
            directory=os.open(owner,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
            try:
                try: fd=os.open(filename,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                except FileExistsError:
                    fd=os.open(filename,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
                    with os.fdopen(fd,'rb') as handle:
                        if hashlib.sha256(handle.read(10*1024*1024+1)).hexdigest()!=sha: raise MigrationError('attachment_destination_conflict')
                else:
                    with os.fdopen(fd,'wb') as handle:
                        handle.write(data); handle.flush(); os.fsync(handle.fileno())
                os.fsync(directory)
            finally: os.close(directory)
    except OSError: raise MigrationError('attachment_destination_error') from None
    finally: os.close(root_fd)
    unique={}
    for step in extra:
        unique.setdefault((step.target,step.values['id']),step)
    result.steps=list(unique.values())+result.steps
    # Clear only those table-level asset blockers with no unresolved image reference.
    def unresolved(source):
        return any(not s.startswith('/static/uploads/replies/') for step in result.steps if step.source==source and (step.target!='xy_reply_events' or step.values['content_type']=='image')
                   for f in FIELDS.get(step.target,()) for s in _strings(step.values.get(f)) if s)
    if any(src=='chat_messages' for src,ref in resolved) and not unresolved('chat_messages'):
        if all(not r.get('extra_json') and not r.get('media_url') for r in prepared.snapshot.tables.get('chat_messages',[])):
            result.issues=[i for i in result.issues if i['code']!='rich_message_attachment_requires_review']
    result.issues=[i for i in result.issues if not (i['code']=='attachment_verification_required'
                   and any(src==i['table'] for src,ref in resolved) and not unresolved(i['table']))]
    store.write_bundle('asset-manifest-'+digest(manifest,store.key)+'.enc',manifest)
    return result
