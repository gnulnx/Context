"""Daemon-owned local retrieval. SQLite is authoritative; Qdrant is rebuildable."""
import hashlib
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from qdrant_client import QdrantClient, models
from tokenizers import Tokenizer

from . import capture, embedding, storage
from .embedding_runtime import load_files
from .preview import preview

MODEL = embedding.MODEL
COLLECTION = 'context_v1'
POLICY = 'preview-v2-visible-token384-overlap48'


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def epoch(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Timestamp must be an ISO string')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class Index:
    def __init__(self, paths):
        self.paths = paths
        storage.prepare_sqlite_wal(paths)
        self.database = storage.database_path(paths)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='context-index')
        self.model = None
        self.vectors = None
        self.stopping = threading.Event()
        self.write_lock = threading.RLock()
        with closing(self.connect()) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS context_sessions (
                    id TEXT PRIMARY KEY, binding_key TEXT UNIQUE NOT NULL, project TEXT,
                    parent_session_id TEXT, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS authored_updates (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS index_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ingest_jobs (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, sources TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS indexed_sources (
                    path TEXT PRIMARY KEY, digest TEXT NOT NULL, bytes INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL, report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS context_messages (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, context_id TEXT NOT NULL,
                    position INTEGER NOT NULL, timestamp REAL, project TEXT, document TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS context_time ON context_messages(timestamp);
                CREATE INDEX IF NOT EXISTS context_turn ON context_messages(context_id);
                CREATE TABLE IF NOT EXISTS context_chunks (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, document TEXT NOT NULL, vector TEXT NOT NULL);
            ''')
            # Upgrade provenance on indexes created before the MCP interface existed.
            migrated_chunks = 0
            for table in ('context_messages', 'context_chunks'):
                migrated = db.execute(f"""UPDATE {table} SET document=json_set(document,
                    '$.source_type','codex_transcript',
                    '$.source_session_id',json_extract(document,'$.session_id'))
                    WHERE json_extract(document,'$.source_type') IS NULL""").rowcount
                if table == 'context_chunks':
                    migrated_chunks = migrated
            if migrated_chunks:
                db.execute("INSERT OR REPLACE INTO index_settings VALUES ('dirty',?)", (uuid.uuid4().hex,))
            configuration = json.dumps({'model': MODEL, 'policy': POLICY, 'dimensions': 384}, sort_keys=True)
            row = db.execute("SELECT value FROM index_settings WHERE key='configuration'").fetchone()
            if row and json.loads(row[0]) == {'model': MODEL, 'policy': 'preview-v1-token384-overlap48', 'dimensions': 384}:
                db.execute("UPDATE index_settings SET value=? WHERE key='configuration'", (configuration,))
            elif row and row[0] != configuration:
                raise RuntimeError('Index model or policy changed; explicit rebuild is required')
            db.execute("INSERT OR IGNORE INTO index_settings VALUES ('configuration', ?)", (configuration,))
            pending = db.execute("SELECT id FROM ingest_jobs WHERE state IN ('queued','running')").fetchall()
        self.executor.submit(self.retry_updates)
        for row in pending:
            self.executor.submit(self.run_job, row[0])

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def submit(self, request):
        operation = request.get('operation')
        if operation == 'index':
            sources = request.get('sources')
            if not isinstance(sources, list) or not sources or len(sources) > 10000:
                raise ValueError('Index requires 1 to 10000 explicit source files')
            if any(not isinstance(p, str) or not Path(p).is_absolute() or Path(p).suffix != '.jsonl' for p in sources):
                raise ValueError('Sources must be absolute JSONL paths')
            identifier = str(uuid.uuid4())
            with closing(self.connect()) as db, db:
                db.execute('INSERT INTO ingest_jobs VALUES (?,?,?,?,?,?)',
                           (identifier, 'queued', json.dumps(sorted(set(sources))), utcnow(), utcnow(), '{}'))
            self.executor.submit(self.run_job, identifier)
            return {'job_id': identifier, 'state': 'queued'}
        if operation in ('open_session', 'log_update'):
            with self.write_lock:
                return self.open_session(request) if operation == 'open_session' else self.log_update(request)
        if operation == 'index_status':
            result = self.status(request.get('job_id'))
            result['capture'] = capture.status(self.paths)
            return result
        return self.executor.submit(self.query, request).result(timeout=30)

    def status(self, job_id=None):
        with closing(self.connect()) as db:
            if job_id:
                row = db.execute('SELECT * FROM ingest_jobs WHERE id=?', (job_id,)).fetchone()
                if not row:
                    raise ValueError('Unknown job ID')
                return {**dict(row), 'sources': json.loads(row['sources']), 'result': json.loads(row['result'])}
            sources = [dict(r) for r in db.execute('SELECT path, bytes, indexed_at, report FROM indexed_sources ORDER BY path')]
            for source in sources:
                source['report'] = json.loads(source['report'])
                try:
                    current = Path(source['path']).stat()
                    source['selection_outdated'] = source['report'].get('selection') != 'visible-progress-v1'
                    source['changed_since_index'] = source['selection_outdated'] or current.st_size != source['bytes'] or current.st_mtime_ns != source['report'].get('source_mtime_ns')
                    source['missing'] = False
                except OSError:
                    source['missing'] = True
                    source['changed_since_index'] = True
            jobs = [dict(r) for r in db.execute('SELECT id,state,updated,result FROM ingest_jobs ORDER BY created DESC LIMIT 10')]
            return {'model': MODEL, 'policy': POLICY, 'authored_updates_pending': db.execute("SELECT count(*) FROM authored_updates WHERE state!='indexed'").fetchone()[0], 'scope': 'Explicitly queued files and authored updates; not a claim of all Codex history', 'sources': sources, 'jobs': jobs,
                    'messages': db.execute('SELECT count(*) FROM context_messages').fetchone()[0],
                    'chunks': db.execute('SELECT count(*) FROM context_chunks').fetchone()[0],
                    'oldest_timestamp': db.execute('SELECT min(timestamp) FROM context_messages').fetchone()[0],
                    'newest_timestamp': db.execute('SELECT max(timestamp) FROM context_messages').fetchone()[0],
                    'unknown_timestamps': db.execute('SELECT count(*) FROM context_messages WHERE timestamp IS NULL').fetchone()[0]}

    def coverage(self):
        status = self.status()
        sources = status.pop('sources')
        status['jobs'] = [{k: job[k] for k in ('id', 'state', 'updated')} for job in status['jobs']]
        status['source_count'] = len(sources)
        status['changed_sources'] = sum(s['changed_since_index'] for s in sources)
        status['missing_sources'] = sum(s['missing'] for s in sources)
        status['partial_sources'] = sum(bool(s['report'].get('unclassified')) for s in sources)
        return status

    def load_model(self):
        embedding.require_active(self.paths)
        if self.model is None:
            embedding.check_files(self.paths)
            self.model = load_files(embedding.directory(self.paths))
            model_dir = Path(self.model.model._model_dir)
            digest = hashlib.sha256()
            for filename in (self.model.model.model_description.model_file, 'tokenizer.json'):
                with (model_dir / filename).open('rb') as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
            fingerprint = digest.hexdigest()
            with closing(self.connect()) as db, db:
                existing = db.execute("SELECT value FROM index_settings WHERE key='model_fingerprint'").fetchone()
                if existing and existing[0] != fingerprint:
                    self.model = None
                    raise RuntimeError('Embedding model files changed; explicit index rebuild is required')
                db.execute("INSERT OR IGNORE INTO index_settings VALUES ('model_fingerprint',?)", (fingerprint,))
            self.tokenizer = Tokenizer.from_str(self.model.model.tokenizer.to_str())
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        return self.model

    def chunk_text(self, text):
        self.load_model()
        offsets = self.tokenizer.encode(text, add_special_tokens=False).offsets
        for start in range(0, len(offsets), 336):
            end = min(start + 384, len(offsets))
            left, right = offsets[start][0], offsets[end - 1][1]
            yield left, right, text[left:right]
            if end == len(offsets):
                break

    def open_vectors(self):
        if self.vectors is None:
            directory, self.vector_baseline = storage.prepare_index_artifacts(self.paths, 'vectors')
            self.vectors = QdrantClient(path=str(directory))
        if not self.vectors.collection_exists(COLLECTION):
            self.vectors.create_collection(COLLECTION, vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
            self.set_dirty(True)
        with closing(self.connect()) as db:
            dirty = db.execute("SELECT value FROM index_settings WHERE key='dirty'").fetchone()
        if dirty and dirty[0] != '0':
            self.vectors.delete_collection(COLLECTION)
            self.vectors.create_collection(COLLECTION, vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
            with closing(self.connect()) as db:
                cursor = db.execute('SELECT * FROM context_chunks')
                while batch := cursor.fetchmany(64):
                    self.vectors.upsert(COLLECTION, [models.PointStruct(id=r['id'], vector=json.loads(r['vector']), payload=json.loads(r['document'])) for r in batch])
            with closing(self.connect()) as db, db:
                db.execute("UPDATE index_settings SET value='0' WHERE key='dirty' AND value=?", (dirty[0],))
        storage.record_index_artifacts(self.paths, 'vectors', self.vector_baseline)
        self.vector_baseline = {str(p.relative_to(self.paths['data'] / 'vectors')) for p in (self.paths['data'] / 'vectors').rglob('*') if not p.is_dir()}
        return self.vectors

    def set_dirty(self, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO index_settings VALUES ('dirty',?)", (uuid.uuid4().hex if value else '0',))

    def run_job(self, job_id):
        with closing(self.connect()) as db, db:
            sources = json.loads(db.execute('SELECT sources FROM ingest_jobs WHERE id=?', (job_id,)).fetchone()[0])
            db.execute("UPDATE ingest_jobs SET state='running',updated=? WHERE id=?", (utcnow(), job_id))
        report = {'files': [], 'failed': []}
        for source in sources:
            if self.stopping.is_set():
                return  # Persist running state; startup replays the idempotent job.
            try:
                report['files'].append(self.index_file(Path(source)))
            except Exception as exc:
                report['failed'].append({'path': source, 'error': str(exc)})
            with closing(self.connect()) as db, db:
                db.execute('UPDATE ingest_jobs SET result=?,updated=? WHERE id=?', (json.dumps(report), utcnow(), job_id))
        partial = report['failed'] or any(r.get('unclassified', 0) for r in report['files'])
        with closing(self.connect()) as db, db:
            db.execute('UPDATE ingest_jobs SET state=?,result=?,updated=? WHERE id=?',
                       ('partial' if partial else 'complete', json.dumps(report), utcnow(), job_id))

    def index_file(self, path, context_session_id=None, capture_generation=None):
        storage.safe_path(path)
        data = preview(path, 1, 2**63, details=True)
        size = data['snapshot_bytes']
        # Hash exactly the parsed snapshot. If it changed in place, retry later.
        before = path.stat()
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            remaining = size
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise RuntimeError('Source shrank during import; retry')
                digest.update(block)
                remaining -= len(block)
        fingerprint = digest.hexdigest()
        after = path.stat()
        if before.st_ino != after.st_ino or after.st_size < size or fingerprint != data['source_digest']:
            raise RuntimeError('Source changed during import; retry')
        with closing(self.connect()) as db:
            previous = db.execute('SELECT * FROM indexed_sources WHERE path=?', (str(path),)).fetchone()
        if previous and context_session_id is None:
            context_session_id = json.loads(previous['report']).get('context_session_id')
        if previous and previous['digest'] == fingerprint and json.loads(previous['report']).get('capture_generation') == capture_generation and json.loads(previous['report']).get('selection') == 'visible-progress-v1':
            return {**json.loads(previous['report']), 'unchanged': True}
        session_id = data['metadata'].get('session_id') or data['metadata'].get('id') or str(path)
        replaced_sources = [str(path)]
        with closing(self.connect()) as db:
            others = db.execute('SELECT path,digest,report FROM indexed_sources WHERE path!=?', (str(path),)).fetchall()
        for other in others:
            if json.loads(other['report']).get('session_id') != session_id:
                continue
            if not Path(other['path']).exists():
                replaced_sources.append(other['path'])
            elif other['digest'] == fingerprint:
                return {**json.loads(other['report']), 'path': str(path), 'unchanged': True, 'duplicate_source_of': other['path']}
            else:
                raise RuntimeError('Conflicting existing transcripts share a session ID; resolve before importing')
        messages, chunks = [], []
        for turn in data['turns']:
            context_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'codex:{session_id}:{turn["turn_id"]}'))
            for record in turn['records']:
                if record['decision'] not in ('index', 'context_only') or not record.get('text'):
                    continue
                identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{context_id}:{record["line"]}'))
                try:
                    timestamp = epoch(record['timestamp'])
                except (ValueError, TypeError):
                    timestamp = None
                doc = {'id': identifier, 'context_id': context_id, 'session_id': session_id,
                       'source_type': 'codex_transcript', 'source_session_id': session_id,
                       'context_session_id': context_session_id,
                       'turn_id': turn['turn_id'], 'project': turn['project'],
                       'timestamp': record['timestamp'], 'timestamp_epoch': timestamp,
                       'role': record['role'], 'phase': record['phase'], 'text': record['text'],
                       'decision': record['decision'], 'source': {'path': str(path), 'line': record['line'], 'byte_offset': record['byte_offset']}}
                if context_session_id and record['role'] == 'assistant':
                    with closing(self.connect()) as db:
                        note = db.execute("SELECT id FROM context_messages WHERE json_extract(document,'$.context_session_id')=? AND json_extract(document,'$.source_type')='authored_update' AND json_extract(document,'$.source_text')=? ORDER BY timestamp DESC LIMIT 1", (context_session_id, record['text'])).fetchone()
                    if note:
                        doc['decision'] = 'context_only'
                        doc['duplicate_of'] = note['id']
                messages.append((identifier, str(path), context_id, record['line'], timestamp, turn['project'], json.dumps(doc)))
                if doc['decision'] == 'index':
                    for left, right, text in self.chunk_text(record['text']):
                        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{identifier}:{left}:{right}:{hashlib.sha256(text.encode()).hexdigest()}'))
                        chunks.append({'id': chunk_id, 'document': {**doc, 'text': text, 'char_start': left, 'char_end': right, 'chunk_id': chunk_id}})
        vectors = self.open_vectors()
        self.set_dirty(True)
        with closing(self.connect()) as db:
            vector_revision = db.execute("SELECT value FROM index_settings WHERE key='dirty'").fetchone()[0]
        rows = []
        for start in range(0, len(chunks), 32):
            batch = chunks[start:start+32]
            placeholders = ','.join('?' for _ in batch)
            with closing(self.connect()) as db:
                cached = {r['id']:json.loads(r['vector']) for r in db.execute(
                    f'SELECT id,vector FROM context_chunks WHERE id IN ({placeholders})', [c['id'] for c in batch])}
            missing = [c for c in batch if c['id'] not in cached]
            if missing:
                embeddings = self.load_model().passage_embed([c['document']['text'] for c in missing], batch_size=32)
                cached.update({chunk['id']:vector.tolist() for chunk,vector in zip(missing,embeddings,strict=True)})
            points = []
            for chunk in batch:
                values = cached[chunk['id']]
                rows.append((chunk['id'], str(path), json.dumps(chunk['document']), json.dumps(values)))
                points.append(models.PointStruct(id=chunk['id'], vector=values, payload=chunk['document']))
            vectors.upsert(COLLECTION, points)
        report = {'path': str(path), 'session_id': session_id, 'messages': len(messages), 'chunks': len(chunks),
                  'snapshot_bytes': size, 'source_mtime_ns': before.st_mtime_ns, 'unclassified': data['counts'].get('unclassified', 0),
                  'counts': data['counts'], 'unchanged': False, 'capture_generation': capture_generation, 'context_session_id': context_session_id, 'selection': 'visible-progress-v1'}
        with (storage.locked(self.paths) if capture_generation else nullcontext()):
            if capture_generation:
                current = storage.read_manifest(self.paths)
                if current['state'] != 'active' or current.get('codex_hooks', {}).get('generation') != capture_generation:
                    raise RuntimeError('Capture disconnected during indexing')
            with closing(self.connect()) as db, db:
                old = set()
                for replaced in replaced_sources:
                    old.update(r[0] for r in db.execute('SELECT id FROM context_chunks WHERE source=?', (replaced,)))
                    db.execute('DELETE FROM context_messages WHERE source=?', (replaced,))
                    db.execute('DELETE FROM context_chunks WHERE source=?', (replaced,))
                    db.execute('DELETE FROM indexed_sources WHERE path=?', (replaced,))
                db.executemany('INSERT INTO context_messages VALUES (?,?,?,?,?,?,?)', messages)
                db.executemany('INSERT INTO context_chunks VALUES (?,?,?,?)', rows)
                if context_session_id:
                    aliases = db.execute("SELECT m.id,m.document,n.id AS note_id FROM context_messages m JOIN context_messages n ON json_extract(n.document,'$.source_type')='authored_update' AND json_extract(n.document,'$.context_session_id')=? AND json_extract(n.document,'$.source_text')=json_extract(m.document,'$.text') WHERE m.source=? AND json_extract(m.document,'$.role')='assistant'", (context_session_id,str(path))).fetchall()
                    for alias in aliases:
                        document = json.loads(alias['document'])
                        document.update(decision='context_only',duplicate_of=alias['note_id'])
                        db.execute('UPDATE context_messages SET document=? WHERE id=?',(json.dumps(document),alias['id']))
                        db.execute("DELETE FROM context_chunks WHERE json_extract(document,'$.id')=?", (alias['id'],))

                db.execute('INSERT OR REPLACE INTO indexed_sources VALUES (?,?,?,?,?)', (str(path), fingerprint, size, utcnow(), json.dumps(report)))
        with closing(self.connect()) as db:
            actual = {r[0] for r in db.execute('SELECT id FROM context_chunks WHERE source=?',(str(path),))}
        obsolete = list((old | {r[0] for r in rows}) - actual)
        if obsolete:
            vectors.delete(COLLECTION, models.PointIdsList(points=obsolete))
        with closing(self.connect()) as db, db:
            db.execute("UPDATE index_settings SET value='0' WHERE key='dirty' AND value=?", (vector_revision,))
        storage.record_index_artifacts(self.paths, 'vectors', self.vector_baseline)
        return report

    @staticmethod
    def identifier(value):
        try:
            return str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Session, binding, and update identifiers must be UUIDs') from None

    def open_session(self, request):
        binding = self.identifier(request.get('binding_key'))
        parent = request.get('parent_session_id')
        if parent is not None:
            parent = self.identifier(parent)
        project = request.get('project')
        if project is not None and (not isinstance(project, str) or not project or len(project) > 4096):
            raise ValueError('project must be a nonempty string of at most 4096 characters')
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT * FROM context_sessions WHERE binding_key=?', (binding,)).fetchone()
            if row:
                if row['project'] != project or row['parent_session_id'] != parent:
                    raise ValueError('Binding already belongs to a session with different metadata')
                return {'session_id': row['id'], 'created_at': row['created'], 'reused': True}
            if parent and not db.execute('SELECT 1 FROM context_sessions WHERE id=?', (parent,)).fetchone():
                raise ValueError('Unknown parent Context session')
            identifier, created = str(uuid.uuid4()), utcnow()
            db.execute('INSERT INTO context_sessions VALUES (?,?,?,?,?)', (identifier, binding, project, parent, created))
        return {'session_id': identifier, 'created_at': created, 'reused': False}

    def log_update(self, request):
        session = self.identifier(request.get('session_id'))
        identifier = self.identifier(request.get('update_id'))
        text = request.get('text')
        tags = request.get('tags') or []
        kind = request.get('kind', 'status')
        authorship = request.get('authorship', 'agent_generated')
        source_text = request.get('source_text')
        if source_text is not None and (not isinstance(source_text, str) or not source_text.strip() or len(source_text) > 16000):
            raise ValueError('source_text must be exact visible message text, at most 16000 characters')
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError('Update text must contain 1..16000 characters')
        if not isinstance(tags, list) or len(tags) > 20 or any(not isinstance(t, str) or not t.strip() or len(t) > 64 for t in tags):
            raise ValueError('Provide at most 20 nonempty tags of at most 64 characters')
        if kind not in ('status', 'decision', 'note') or authorship not in ('agent_generated', 'user_requested'):
            raise ValueError('Invalid kind or authorship')
        payload = json.dumps(dict(session_id=session, text=text, tags=sorted(set(tags)), kind=kind, authorship=authorship, source_text=source_text), sort_keys=True)
        with closing(self.connect()) as db, db:
            owner = db.execute('SELECT * FROM context_sessions WHERE id=?', (session,)).fetchone()
            if not owner:
                raise ValueError('Unknown Context session; call open_session first')
            previous = db.execute('SELECT * FROM authored_updates WHERE id=?', (identifier,)).fetchone()
            previous_payload = json.loads(previous['payload']) if previous else None
            if previous_payload is not None:
                previous_payload.setdefault('source_text', None)
            if previous and previous_payload != json.loads(payload):
                raise ValueError('Update ID already exists with different content')
            if not previous:
                now = utcnow()
                doc = dict(id=identifier, context_id=identifier, context_session_id=session,
                           source_type='authored_update', source={'update_id': identifier},
                           timestamp=now, timestamp_epoch=epoch(now), project=owner['project'],
                           role='assistant', phase=None, text=text, tags=sorted(set(tags)),
                           kind=kind, authorship=authorship, source_text=source_text, decision='index')
                db.execute('INSERT INTO authored_updates VALUES (?,?,?,NULL)', (identifier, payload, 'pending'))
                db.execute('INSERT INTO context_messages VALUES (?,?,?,?,?,?,?)',
                           (identifier, 'update:'+identifier, identifier, 0, epoch(now), owner['project'], json.dumps(doc)))
        if source_text is not None:
            # Fast SQL-only aliasing; vector repair is scheduled on the embedding worker.
            with closing(self.connect()) as db, db:
                rows = db.execute("SELECT id,document FROM context_messages WHERE json_extract(document,'$.source_type')='codex_transcript' AND json_extract(document,'$.context_session_id')=? AND json_extract(document,'$.role')='assistant' AND json_extract(document,'$.text')=?", (session,source_text)).fetchall()
                for row in rows:
                    document = json.loads(row['document'])
                    document.update(decision='context_only', duplicate_of=identifier)
                    db.execute('UPDATE context_messages SET document=? WHERE id=?',(json.dumps(document),row['id']))
                    db.execute("DELETE FROM context_chunks WHERE json_extract(document,'$.id')=?", (row['id'],))
                if rows:
                    db.execute("INSERT OR REPLACE INTO index_settings VALUES ('dirty',?)", (uuid.uuid4().hex,))
        # Persist the note before scheduling embeddings; downloads never delay acknowledgement.
        self.executor.submit(self.embed_update, identifier)
        with closing(self.connect()) as db:
            row = db.execute('SELECT state,error FROM authored_updates WHERE id=?', (identifier,)).fetchone()
        return {'update_id': identifier, 'context_id': identifier, 'session_id': session,
                'stored': True, 'reused': previous is not None, 'indexing_state': row['state'], 'indexing_error': row['error']}

    def retry_updates(self):
        with closing(self.connect()) as db:
            pending = db.execute("SELECT id FROM authored_updates WHERE state!='indexed'").fetchall()
        for row in pending:
            if self.stopping.is_set():
                break
            self.embed_update(row['id'])

    def embed_update(self, identifier):
        try:
            with closing(self.connect()) as db:
                if db.execute('SELECT state FROM authored_updates WHERE id=?', (identifier,)).fetchone()[0] == 'indexed':
                    return
                doc = json.loads(db.execute('SELECT document FROM context_messages WHERE id=?', (identifier,)).fetchone()[0])
            rows = []
            for left, right, text in self.chunk_text(doc['text']):
                chunk_id = str(uuid.uuid5(uuid.UUID(identifier), f'{left}:{right}'))
                payload = {**doc, 'text': text, 'char_start': left, 'char_end': right, 'chunk_id': chunk_id}
                vector = next(self.load_model().passage_embed([text])).tolist()
                rows.append((chunk_id, 'update:'+identifier, json.dumps(payload), json.dumps(vector)))
            vectors = self.open_vectors()
            revision = uuid.uuid4().hex
            with closing(self.connect()) as db, db:
                db.executemany('INSERT OR REPLACE INTO context_chunks VALUES (?,?,?,?)', rows)
                db.execute("INSERT OR REPLACE INTO index_settings VALUES ('dirty',?)", (revision,))
            vectors.upsert(COLLECTION, [models.PointStruct(id=r[0],vector=json.loads(r[3]),payload=json.loads(r[2])) for r in rows])
            with closing(self.connect()) as db, db:
                db.execute("UPDATE index_settings SET value='0' WHERE key='dirty' AND value=?", (revision,))
                db.execute("UPDATE authored_updates SET state='indexed',error=NULL WHERE id=?", (identifier,))
            storage.record_index_artifacts(self.paths, 'vectors', self.vector_baseline)
        except Exception as exc:
            with closing(self.connect()) as db, db:
                db.execute("UPDATE authored_updates SET state='pending',error=? WHERE id=?", (str(exc), identifier))

    def query(self, request):
        operation = request.get('operation')
        if operation == 'open_session':
            return self.open_session(request)
        if operation == 'log_update':
            return self.log_update(request)
        limit = request.get('limit', 10)
        offset = request.get('offset', 0)
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError('limit must be 1..50 and offset 0..100000')
        if operation not in ('recent_context', 'search_context', 'get_context'):
            raise ValueError('Unknown operation')
        char_offset = request.get('char_offset', 0)
        if type(char_offset) is not int or char_offset < 0 or (char_offset and (operation != 'get_context' or limit != 1)):
            raise ValueError('char_offset requires get_context with limit=1')
        if request.get('since') and request.get('until') and epoch(request['since']) >= epoch(request['until']):
            raise ValueError('since must be earlier than until')
        clauses, params = [], []
        for name, comparison in [('since', '>='), ('until', '<')]:
            if request.get(name):
                clauses.append(f'timestamp {comparison} ?')
                params.append(epoch(request[name]))
        if request.get('project'):
            clauses.append('project=?')
            params.append(request['project'])
        where = ' AND '.join(clauses) or '1'
        with closing(self.connect()) as db:
            if operation == 'get_context':
                rows = db.execute('SELECT document FROM context_messages WHERE context_id=? ORDER BY position LIMIT ? OFFSET ?', (request.get('context_id'), limit+1, offset)).fetchall()
                documents = [json.loads(r[0]) for r in rows]
            elif operation == 'recent_context':
                # Only searchable messages earn a turn a place in recent results.
                rows = db.execute(f'''SELECT context_id, max(timestamp) AS latest FROM context_messages
                    WHERE {where} AND json_extract(document,'$.decision')='index'
                    GROUP BY context_id ORDER BY latest DESC, context_id LIMIT ? OFFSET ?''', (*params, limit+1, offset)).fetchall()
                documents = []
                for row in rows:
                    messages = db.execute(f"SELECT document FROM context_messages WHERE context_id=? AND {where} AND json_extract(document,'$.decision')='index' ORDER BY position LIMIT 21", (row['context_id'], *params)).fetchall()
                    documents.append({'context_id': row['context_id'], 'latest_timestamp': row['latest'], 'messages': [json.loads(m[0]) for m in messages[:20]], 'messages_truncated': len(messages) > 20})
            else:
                query = request.get('query')
                if not isinstance(query, str) or not query.strip() or len(query) > 2000:
                    raise ValueError('Query must contain 1..2000 characters')
                if not db.execute('SELECT 1 FROM context_chunks LIMIT 1').fetchone():
                    return {'results': [], 'has_more': False, 'coverage': self.coverage()}
                filters = []
                if request.get('project'):
                    filters.append(models.FieldCondition(key='project', match=models.MatchValue(value=request['project'])))
                if request.get('since') or request.get('until'):
                    bounds = models.Range(gte=epoch(request.get('since')), lt=epoch(request.get('until')))
                    filters.append(models.FieldCondition(key='timestamp_epoch', range=bounds))
                vector = next(self.load_model().query_embed(query)).tolist()
                points = self.open_vectors().query_points(COLLECTION, query=vector, query_filter=models.Filter(must=filters) if filters else None, limit=limit+1, offset=offset).points
                documents = [{**p.payload, 'score': p.score} for p in points]
        with closing(self.connect()) as db:
            def attach(document):
                for message in document.get('messages', []):
                    attach(message)
                if document.get('source_type') == 'authored_update':
                    references = db.execute("SELECT document FROM context_messages WHERE json_extract(document,'$.duplicate_of')=? LIMIT 21", (document['id'],)).fetchall()
                    document['source_references'] = [{'context_id':d['context_id'], 'timestamp':d.get('timestamp'), **d['source']} for r in references[:20] for d in [json.loads(r[0])]]
                    document['source_references_truncated'] = len(references) > 20
            for document in documents:
                attach(document)
        more = len(documents) > limit
        documents = documents[:limit]
        # Bound text without silently representing excerpts as complete messages.
        remaining = 24000
        def bound(document):
            nonlocal remaining
            if 'messages' in document:
                for message in document['messages']:
                    bound(message)
            if 'text' in document:
                text = document['text']
                document['text'] = text[char_offset:char_offset+remaining]
                document['text_truncated'] = len(text) > char_offset+remaining
                document['text_char_start'] = char_offset
                document['text_char_end'] = char_offset + len(document['text'])
                document['next_char_offset'] = document['text_char_end'] if document['text_truncated'] else None
                remaining -= len(document['text'])
        for document in documents:
            bound(document)
        return {'results': documents, 'has_more': more, 'next_offset': offset+limit if more else None,
                'coverage': self.coverage(), 'text_budget': 24000}

    def close(self):
        self.stopping.set()
        def close_vectors():
            if self.vectors:
                self.vectors.close()
        self.executor.submit(close_vectors).result()
        self.executor.shutdown(wait=True, cancel_futures=True)
