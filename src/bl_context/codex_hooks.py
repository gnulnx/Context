"""Owned hooks.json registration and independently observed trust/capture checks."""
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import shlex
import tempfile
import uuid

from . import capture, storage
from .mcp_registration import root, config
from .codex_probe import query


def read(path):
    storage.safe_path(path)
    if not path.exists():
        return {}
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or not isinstance(document.get('hooks',{}),dict):
        raise RuntimeError('Invalid hooks.json; preserving it')
    return document


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.blctx-hooks-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(document,stream,indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
        storage.sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def handler_version():
    digest = hashlib.sha256()
    for name in ('capture.py', 'hook_handler.py'):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def entries(paths, manifest, generation):
    env = [f'XDG_{key.upper()}_HOME={value.parent}' for key,value in paths.items()]
    command = shlex.join(['/usr/bin/env',*env,manifest['interpreter'],'-m','bl_context.hook_handler',
                          '--installation-id',manifest['installation_id'],'--generation',generation,'--handler-version',handler_version()])
    return {event:{'hooks':[{'type':'command','command':command,'timeout':2}]} for event in capture.EVENTS}


def remove_owned(document, owned):
    for event, entry in owned['entries'].items():
        groups = document.get('hooks',{}).get(event,[])
        if entry in groups:
            groups.remove(entry)
            if not groups:
                document['hooks'].pop(event)
        elif any('bl_context.hook_handler' in json.dumps(group) for group in groups):
            raise RuntimeError('Owned Context hook was modified; preserving it')
    return document


def install():
    storage.verify()
    paths = storage.locations()
    directory = root()
    path = directory/'hooks.json'
    if set(config(directory).get('hooks', {})) - {'state'}:
        raise RuntimeError('Inline Codex hooks are configured; consolidate into hooks.json before installing Context hooks')
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        owned = manifest.get('codex_hooks')
        if owned and owned['root'] != str(directory):
            raise RuntimeError('Hooks belong to another CODEX_HOME; uninstall that registration first')
        document = read(path)
        if owned:
            remove_owned(document,owned)
        if 'bl_context.hook_handler' in json.dumps(document):
            raise RuntimeError('Unowned Context hook collision; preserving configuration')
        generation = owned['generation'] if owned else str(uuid.uuid4())
        expected = entries(paths,manifest,generation)
        for event, entry in expected.items():
            document.setdefault('hooks',{}).setdefault(event,[]).append(entry)
        if not owned:
            with closing(capture.connect(paths)) as db, db:
                db.executescript(capture.SCHEMA)
                db.execute('DELETE FROM capture_events')
                db.execute('UPDATE capture_bindings SET transcript=NULL,signature=NULL,indexed_at=NULL,error=NULL')
        write(path,document)
        manifest['codex_hooks'] = {'root':str(directory),'path':str(path),'entries':expected,'generation':generation}
        storage.atomic_manifest(paths,manifest)
    # Trust must be granted by the engineer in Codex /hooks, never by the installer.


def verify():
    storage.verify()
    from . import service
    service.verify()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    owned = manifest.get('codex_hooks')
    if not owned or owned['root'] != str(root()):
        raise RuntimeError('Context hooks are not registered for this CODEX_HOME')
    document = read(Path(owned['path']))
    features = config(root()).get('features', {})
    if features.get('hooks') is False or features.get('codex_hooks') is False:
        raise RuntimeError('Codex hooks feature is disabled; preserving that preference')
    if set(config(root()).get('hooks', {})) - {'state'}:
        raise RuntimeError('Inline hooks shadow or conflict with hooks.json')
    for event, entry in entries(paths,manifest,owned['generation']).items():
        if owned['entries'].get(event) != entry or document.get('hooks',{}).get(event,[]).count(entry) != 1:
            raise RuntimeError('Context hook registration is missing, modified, or stale')
    discovered = query('hooks/list',root(),Path.home())
    hooks = [h for row in discovered['data'] for h in row['hooks']
             if h.get('command') == owned['entries']['SessionStart']['hooks'][0]['command']]
    if len(hooks) != 3 or any(not h['enabled'] or h['trustStatus'] != 'trusted' for h in hooks):
        raise RuntimeError('Context hooks need Codex trust or are disabled. Open Codex /hooks and review all three Context hooks')
    report = capture.status(paths)
    if any(not report['events'].get(event) for event in capture.EVENTS) or not report['indexed_sessions']:
        raise RuntimeError('Hooks trusted; real delivery/indexing still pending. Start a new Codex conversation, complete a turn, then close it normally')
    if report['errors'] or report['pending']:
        raise RuntimeError(f'Hook capture pending or failed: {report}')
    return 'Three trusted Codex hooks, lifecycle delivery, and background capture verified.'


def uninstall():
    paths = storage.locations()
    if not storage.manifest_path(paths).exists():
        return
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        owned = manifest.get('codex_hooks')
        if not owned:
            return
        path = Path(owned['path'])
        if path != Path(owned['root'])/'hooks.json':
            raise RuntimeError('Invalid hook ownership path')
        document = remove_owned(read(path),owned)
        if path.exists():
            write(path,document)
        manifest.pop('codex_hooks')
        storage.atomic_manifest(paths,manifest)
        with closing(capture.connect(paths)) as db, db:
            db.executescript(capture.SCHEMA)
            db.execute('UPDATE capture_bindings SET transcript=NULL,signature=NULL,indexed_at=NULL,error=NULL')
            db.execute('DELETE FROM capture_events')
