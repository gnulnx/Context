"""Read-only transcript discovery with a separately activated SQLite inventory."""
from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from . import storage
from .index import utcnow
from .preview import classify_transcript

SCHEMA = '''
CREATE TABLE IF NOT EXISTS discovery_runs (id TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_sources (
 inventory_id TEXT NOT NULL, path TEXT NOT NULL, document TEXT NOT NULL,
 PRIMARY KEY(inventory_id,path));
'''


def root():
    return Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().absolute()


def connect(paths, readonly=False):
    path = storage.database_path(paths)
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) if readonly else sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def files(base):
    storage.safe_path(base)
    if not base.is_dir():
        raise RuntimeError(f'Codex root is missing: {base}. Set CODEX_HOME to the local history root.')
    found = []
    def error(exc):
        raise exc
    for kind in ('sessions', 'archived_sessions'):
        folder = base/kind
        storage.safe_path(folder)
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise RuntimeError(f'History directory is not a directory: {folder}')
        for parent, directories, names in os.walk(folder, followlinks=False, onerror=error):
            for name in directories:
                storage.safe_path(Path(parent)/name)
            for name in names:
                if name.endswith('.jsonl'):
                    path = Path(parent)/name
                    storage.safe_path(path)
                    if not path.is_file():
                        raise RuntimeError(f'History source is not a regular file: {path}')
                    found.append((path, kind == 'archived_sessions'))
    return sorted(found)


def prefix_hash(path, size):
    storage.safe_path(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        remaining = size
        while remaining:
            block = stream.read(min(remaining, 1024*1024))
            if not block:
                raise RuntimeError(f'History source shrank: {path}')
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def inspect(path, archived):
    before = path.stat()
    timestamps, projects, counts = [], set(), Counter()
    def collect(turn):
        if isinstance(turn['project'], str) and len(turn['project']) <= 4096:
            projects.add(turn['project'])
        elif turn['project'] is not None:
            counts['invalid_project'] += 1
        for record in turn['records']:
            counts[record['decision']] += 1
            if record['decision'] in ('index','context_only') and record['timestamp'] is None:
                counts['unknown_timestamp'] += 1
            if isinstance(record['timestamp'], str):
                from .index import epoch
                try:
                    timestamps.append(epoch(record['timestamp']))
                except ValueError:
                    counts['invalid_timestamp'] += 1
    data = classify_transcript(path, on_turn=collect)
    meta = data['metadata']
    session = meta.get('session_id') or meta.get('id')
    if not isinstance(session, str) or not session or len(session) > 512:
        session = None
    if prefix_hash(path, data['snapshot_bytes']) != data['source_digest']:
        raise RuntimeError(f'Source changed while discovering: {path}; rerun discovery.')
    return dict(path=str(path), archived=archived, source_id=hashlib.sha256(str(path).encode()).hexdigest(),
                session_id=session, parent_session_id=meta.get('forked_from_id') or meta.get('parent_session_id'),
                snapshot_bytes=data['snapshot_bytes'], digest=data['source_digest'],
                mtime_ns=before.st_mtime_ns, created_at=meta.get('timestamp'),
                first_timestamp=min(timestamps, default=None), last_timestamp=max(timestamps, default=None),
                project=meta.get('cwd'), projects=sorted(projects), cli_version=meta.get('cli_version'),
                counts=dict(counts), compatible=bool(session) and not counts['invalid_project'], adapter='codex-jsonl-v2',
                error=None if session and not counts['invalid_project'] else 'Invalid session identity or project; source cannot be indexed safely')


def install():
    from .history import emit
    paths = storage.locations()
    storage.verify()
    storage.prepare_sqlite_wal(paths)
    base = root()
    entries = files(base)
    identifier = str(uuid.uuid4())
    counts = Counter()
    rows = []
    for n, (path, archived) in enumerate(entries):
        emit('Discovering sessions', n, len(entries))
        try:
            document = inspect(path, archived)
        except (OSError, RuntimeError) as exc:
            document = dict(path=str(path), archived=archived, compatible=False, error=str(exc), counts={})
        counts.update(document['counts'])
        rows.append(document)
    # Repeated installation of an identical inventory resumes the existing job.
    manifest = storage.read_manifest(paths)
    previous = manifest.get('session_discovery')
    if previous and previous['root'] == str(base):
        try:
            _, old = inventory(paths)
        except (RuntimeError, ValueError, sqlite3.Error):
            old = None
        if old == sorted(rows,key=lambda r:r['path']):
            emit('Discovery unchanged', len(entries), len(entries))
            return
    with closing(connect(paths)) as db, db:
        db.executescript(SCHEMA)
        db.executemany('INSERT INTO discovery_sources VALUES (?,?,?)',
                       [(identifier,r['path'],json.dumps(r)) for r in rows])
        summary = dict(id=identifier, root=str(base), created_at=utcnow(), files=len(rows),
                       sessions=len({r['session_id'] for r in rows if r.get('session_id')}),
                       active=sum(not r['archived'] for r in rows), archived=sum(r['archived'] for r in rows),
                       unsupported=sum(not r['compatible'] for r in rows), counts=dict(counts),
                       missing_directories=[kind for kind in ('sessions','archived_sessions') if not (base/kind).exists()],
                       empty=not rows, adapter='codex-jsonl-v2')
        db.execute('INSERT INTO discovery_runs VALUES (?,?)', (identifier,json.dumps(summary)))
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        if manifest['state'] != 'active':
            raise RuntimeError('Discovery disconnected during scan')
        manifest['session_discovery'] = {'id': identifier, 'root': str(base)}
        manifest.pop('history_index', None)
        storage.atomic_manifest(paths, manifest)
    emit('Discovery complete', len(entries), len(entries))


def inventory(paths=None, *, match_root=True):
    paths = paths or storage.locations()
    storage.verify()
    manifest = storage.read_manifest(paths)
    registration = manifest.get('session_discovery')
    if not registration or (match_root and registration['root'] != str(root())):
        raise RuntimeError('No active inventory for this CODEX_HOME. Run blctx install codex --step session_discovery.')
    with closing(connect(paths, readonly=True)) as db:
        row = db.execute('SELECT document FROM discovery_runs WHERE id=?', (registration['id'],)).fetchone()
        if not row:
            raise RuntimeError('Registered discovery inventory is missing')
        summary = json.loads(row[0])
        rows = [json.loads(r[0]) for r in db.execute('SELECT document FROM discovery_sources WHERE inventory_id=? ORDER BY path', (registration['id'],))]
    if summary['files'] != len(rows) or summary['id'] != registration['id'] or summary['root'] != registration['root']:
        raise RuntimeError('Inventory count or identity mismatch')
    counts = Counter()
    for row in rows:
        counts.update(row['counts'])
    if (summary['counts'] != dict(counts)
            or summary['sessions'] != len({r['session_id'] for r in rows if r.get('session_id')})
            or summary['active'] != sum(not r['archived'] for r in rows)
            or summary['archived'] != sum(r['archived'] for r in rows)
            or summary['unsupported'] != sum(not r['compatible'] for r in rows)):
        raise RuntimeError('Inventory counts do not match persisted source entries')
    return summary, rows


def freshness(summary, rows):
    current = {str(path) for path, _ in files(Path(summary['root']))}
    discovered = {r['path'] for r in rows}
    appended = 0
    for row in rows:
        if not row.get('digest'):
            continue
        path = Path(row['path'])
        if str(path) not in current or prefix_hash(path,row['snapshot_bytes']) != row['digest']:
            raise RuntimeError(f'Discovered snapshot missing or changed: {path}; rerun discovery.')
        appended += path.stat().st_size > row['snapshot_bytes']
    return {'new_files': len(current-discovered), 'appended_files': appended}


def verify():
    summary, rows = inventory()
    fresh = freshness(summary, rows)
    return (f"Readable snapshot: {summary['sessions']} sessions in {summary['files']} files "
            f"({summary['active']} active, {summary['archived']} archived); "
            f"unsupported={summary['unsupported']}, unclassified={summary['counts'].get('unclassified',0)}. "
            f"Outside snapshot: {fresh['new_files']} new files, {fresh['appended_files']} appended files. "
            + ('Empty history; no historical retrieval claimed.' if summary['empty'] else ''))
