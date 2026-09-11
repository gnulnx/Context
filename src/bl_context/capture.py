"""Short hook-side SQLite writes; daemon-side incremental reconciliation."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from . import storage

SCHEMA = '''
CREATE TABLE IF NOT EXISTS capture_bindings (
 runtime_key TEXT PRIMARY KEY, binding_key TEXT UNIQUE NOT NULL, project TEXT NOT NULL,
 transcript TEXT, signature TEXT, indexed_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS capture_events (
 id TEXT PRIMARY KEY, runtime_key TEXT NOT NULL, event TEXT NOT NULL,
 received REAL NOT NULL, processed REAL);
'''
EVENTS = ('SessionStart', 'Stop', 'SessionEnd')


def connect(paths):
    db = sqlite3.connect(storage.database_path(paths), timeout=.2)
    db.row_factory = sqlite3.Row
    return db


def signature(path):
    info = path.stat()
    return f'{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'


def receive(event, installation_id, generation):
    paths = storage.locations()
    with storage.locked(paths, timeout=.2):
        manifest = storage.read_manifest(paths)
        owned = manifest.get('codex_hooks')
        if (manifest['state'] != 'active' or manifest['installation_id'] != installation_id
                or not owned or owned.get('generation') != generation):
            return {}  # An already-running old hook cannot revive an uninstalled integration.
        if not isinstance(event, dict) or event.get('hook_event_name') not in EVENTS:
            raise ValueError('Unsupported hook event')
        runtime = event.get('session_id')
        cwd = event.get('cwd')
        transcript = event.get('transcript_path')
        if not isinstance(runtime, str) or not runtime or len(runtime) > 512:
            raise ValueError('Hook requires a bounded session lookup identifier')
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or len(cwd) > 4096:
            raise ValueError('Hook requires an absolute project directory')
        if transcript is not None:
            if not isinstance(transcript, str) or not Path(transcript).is_absolute() or not transcript.endswith('.jsonl'):
                raise ValueError('Transcript must be an absolute JSONL path or null')
            storage.safe_path(Path(transcript))
        runtime_key = hashlib.sha256((owned['root']+'\0'+runtime).encode()).hexdigest()
        stamp = None
        if transcript:
            try:
                stamp = signature(Path(transcript))
            except OSError:
                pass
        event_id = hashlib.sha256(json.dumps([runtime_key,event['hook_event_name'],event.get('source'),
                      event.get('turn_id'),transcript,stamp],sort_keys=True).encode()).hexdigest()
        with closing(connect(paths)) as db, db:
            db.executescript(SCHEMA)
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM capture_bindings WHERE runtime_key=?',(runtime_key,)).fetchone()
            binding = previous['binding_key'] if previous else str(uuid.uuid4())
            project = previous['project'] if previous else cwd
            db.execute('''INSERT INTO capture_bindings VALUES (?,?,?,?,NULL,NULL,NULL)
                ON CONFLICT(runtime_key) DO UPDATE SET transcript=coalesce(excluded.transcript,capture_bindings.transcript)''',
                (runtime_key,binding,project,transcript))
            db.execute('INSERT OR IGNORE INTO capture_events VALUES (?,?,?,?,NULL)',
                       (event_id,runtime_key,event['hook_event_name'],time.time()))
    if event['hook_event_name'] == 'SessionStart':
        context = (
            'Base Layer Context lifecycle binding (not historical transcript text). '
            'For memory-dependent requests, search global Context before answering; do not guess. '
            'Read-only recall does not require open_session and should omit the project filter unless the user explicitly asks for project-only results. '
            f'Before the first log_update only, call base-layer-context.open_session with binding_key={json.dumps(binding)} '
            f'and project={json.dumps(project)}. Reuse the returned Context session_id for later writes in this conversation. '
            'On resume or compaction reopen the same binding; do not invent a new identity. '
            'Use the base-layer-context skill for explicit saves and durable decisions or outcomes. '
            'Copy visible progress text exactly into source_text when logging it. Never log private thinking, '
            'reasoning records, raw tool output, or setup instructions. Do not wait for embeddings. '
            'If Context is unavailable, continue the user task and report the limitation without repeated retries.'
        )
        return {'hookSpecificOutput':{'hookEventName':'SessionStart','additionalContext':context}}
    return {}


def reconcile(engine):
    """Runs on the vector owner thread. Rechecks only explicitly registered transcripts."""
    paths = engine.paths
    with closing(connect(paths)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='capture_bindings'").fetchone():
            return
        rows = db.execute('SELECT * FROM capture_bindings').fetchall()
    for row in rows:
        if engine.stopping.is_set():
            return
        # The uninstall fence serializes capture with removal. No embeddings under it.
        manifest = storage.read_manifest(paths)
        if manifest['state'] != 'active' or not manifest.get('codex_hooks'):
            return
        if not row['transcript']:
            continue
        path = Path(row['transcript'])
        try:
            stamp = signature(path)
            if stamp != row['signature'] or row['error']:
                session = engine.submit({'operation':'open_session','binding_key':row['binding_key'],'project':row['project']})
                report = engine.index_file(path, context_session_id=session['session_id'], capture_generation=manifest['codex_hooks']['generation'])
                with storage.locked(paths):
                    current = storage.read_manifest(paths)
                    if current.get('codex_hooks', {}).get('generation') != manifest['codex_hooks']['generation']:
                        return
                    with closing(connect(paths)) as db, db:
                        db.execute('UPDATE capture_bindings SET signature=?,indexed_at=?,error=NULL WHERE runtime_key=?',
                                   (stamp,time.time() if report.get('messages') else None,row['runtime_key']))
            with storage.locked(paths):
                current = storage.read_manifest(paths)
                if current.get('codex_hooks', {}).get('generation') != manifest['codex_hooks']['generation']:
                    return
                with closing(connect(paths)) as db, db:
                    db.execute('UPDATE capture_events SET processed=? WHERE runtime_key=? AND processed IS NULL',
                               (time.time(),row['runtime_key']))
        except Exception as exc:
            with storage.locked(paths):
                current = storage.read_manifest(paths)
                if current.get('codex_hooks', {}).get('generation') != manifest['codex_hooks']['generation']:
                    return
                with closing(connect(paths)) as db, db:
                    db.execute('UPDATE capture_bindings SET error=? WHERE runtime_key=?',(str(exc),row['runtime_key']))


def status(paths):
    with closing(connect(paths)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='capture_events'").fetchone():
            return {'events':{},'indexed_sessions':0,'pending':0,'errors':0,'null_transcripts':0}
        events = {r[0]:r[1] for r in db.execute('SELECT event,count(*) FROM capture_events GROUP BY event')}
        return {'events':events,
            'indexed_sessions':db.execute('SELECT count(*) FROM capture_bindings WHERE indexed_at IS NOT NULL').fetchone()[0],
            'pending':db.execute('SELECT count(*) FROM capture_events WHERE processed IS NULL').fetchone()[0],
            'errors':db.execute('SELECT count(*) FROM capture_bindings WHERE error IS NOT NULL').fetchone()[0],
            'null_transcripts':db.execute('SELECT count(*) FROM capture_bindings WHERE transcript IS NULL').fetchone()[0]}
