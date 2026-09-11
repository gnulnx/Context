"""Real embedding/retrieval acceptance runs explicitly; no network in normal CI."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from bl_context import storage
from bl_context.index import QUERY_WORKERS, Index, epoch
from bl_context.service import identity


def source(path, project='/project/a', text='We chose lithium batteries for the rover power supply.', day='2026-09-08'):
    rows = [
        {'type': 'session_meta', 'payload': {'id': path.stem, 'cwd': project}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
        {'type': 'response_item', 'timestamp': day+'T12:00:00Z', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}},
        {'type': 'response_item', 'timestamp': day+'T12:01:00Z', 'payload': {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'The battery choice is recorded.'}]}},
    ]
    path.write_text('\n'.join(map(json.dumps, rows))+'\n')


def test_index_status_and_query_validation(tmp_path):
    storage.install()
    engine = Index(storage.locations())
    try:
        assert engine.status()['messages'] == 0
        assert engine.query({'operation':'recent_context'})['results'] == []
        with pytest.raises(ValueError):
            engine.query({'operation':'search_context','query':'', 'limit':100})
        with pytest.raises(ValueError):
            engine.submit({'operation':'index','sources':['relative.jsonl']})
        assert epoch('2026-09-08') == epoch('2026-09-08T00:00:00Z')
    finally:
        engine.close()


def insert_message(engine, identifier, project, text, timestamp):
    document = {
        'id': identifier,
        'context_id': identifier,
        'source_type': 'authored_update',
        'source': {'update_id': identifier},
        'timestamp': timestamp,
        'timestamp_epoch': epoch(timestamp),
        'project': project,
        'role': 'assistant',
        'text': text,
        'decision': 'index',
    }
    with closing(engine.connect()) as db, db:
        db.execute(
            'INSERT INTO context_messages VALUES (?,?,?,?,?,?,?)',
            (
                identifier,
                'update:' + identifier,
                identifier,
                0,
                epoch(timestamp),
                project,
                json.dumps(document),
            ),
        )


def test_lexical_recall_prefers_deliberate_notes_over_raw_transcript_echoes():
    storage.install()
    engine = Index(storage.locations())
    try:
        insert_message(
            engine,
            str(uuid.uuid4()),
            '/project/a',
            'The deployment target is Atlas.',
            '2026-09-10T12:00:00Z',
        )
        raw_id = str(uuid.uuid4())
        with closing(engine.connect()) as db, db:
            document = {
                'id': raw_id,
                'context_id': raw_id,
                'source_type': 'codex_transcript',
                'source': {'path': '/transcript.jsonl', 'line': 1},
                'timestamp': '2026-09-10T13:00:00Z',
                'timestamp_epoch': epoch('2026-09-10T13:00:00Z'),
                'project': '/project/a',
                'role': 'user',
                'text': 'What is the deployment target?',
                'decision': 'index',
            }
            db.execute(
                'INSERT INTO context_messages VALUES (?,?,?,?,?,?,?)',
                (
                    raw_id,
                    '/transcript.jsonl',
                    raw_id,
                    1,
                    document['timestamp_epoch'],
                    document['project'],
                    json.dumps(document),
                ),
            )
        engine.set_dirty(True)

        result = engine.submit(
            {'operation': 'search_context', 'query': 'deployment target'}
        )
        assert result['results'][0]['text'] == 'The deployment target is Atlas.'
        assert result['results'][0]['source_type'] == 'authored_update'
    finally:
        engine.close()


def test_global_recall_is_default_and_project_filter_is_opt_in():
    storage.install()
    engine = Index(storage.locations())
    try:
        insert_message(
            engine,
            str(uuid.uuid4()),
            '/project/a',
            'The magic word is Kaboose.',
            '2026-09-10T12:00:00Z',
        )
        insert_message(
            engine,
            str(uuid.uuid4()),
            '/project/b',
            'Telemetry work finished in the robot project.',
            '2026-09-10T13:00:00Z',
        )
        engine.set_dirty(True)

        global_search = engine.submit(
            {'operation': 'search_context', 'query': 'magic word'}
        )
        assert global_search['results'][0]['text'] == 'The magic word is Kaboose.'
        assert global_search['results'][0]['project'] == '/project/a'
        assert global_search['results'][0]['retrieval_mode'] == 'lexical'
        assert global_search['coverage']['query_mode'] == 'lexical_fallback'
        assert engine.submit(
            {
                'operation': 'search_context',
                'query': 'magic word',
                'project': '/project/b',
            }
        )['results'] == []

        recent = engine.submit(
            {
                'operation': 'recent_context',
                'since': '2026-09-10T00:00:00Z',
                'until': '2026-09-11T00:00:00Z',
            }
        )
        assert {item['messages'][0]['project'] for item in recent['results']} == {
            '/project/a',
            '/project/b',
        }
    finally:
        engine.close()


def test_recall_does_not_wait_for_a_busy_writer():
    storage.install()
    engine = Index(storage.locations())
    release = threading.Event()
    started = threading.Event()

    def busy_writer():
        started.set()
        release.wait(5)

    engine.write_executor.submit(busy_writer)
    assert started.wait(2)
    try:
        insert_message(
            engine,
            str(uuid.uuid4()),
            '/project/a',
            'The magic word is Kaboose.',
            '2026-09-10T12:00:00Z',
        )
        engine.set_dirty(True)
        started_at = time.monotonic()
        assert engine.submit(
            {'operation': 'search_context', 'query': 'magic word'}
        )['results']
        assert engine.submit({'operation': 'recent_context'})['results']
        assert engine.submit({'operation': 'index_status'})['index_state'] == 'syncing'
        assert time.monotonic() - started_at < 1
        assert engine.query_executor._max_workers == QUERY_WORKERS
        assert engine.write_executor._max_workers == 1
    finally:
        release.set()
        engine.close()


def test_dirty_repair_keeps_the_active_collection_and_prunes_only_stale_points():
    kept = str(uuid.uuid4())
    stale = str(uuid.uuid4())

    class Vectors:
        def __init__(self):
            self.upserts = []
            self.deleted = []

        def collection_exists(self, _collection):
            return True

        def upsert(self, _collection, points):
            self.upserts.extend(points)

        def scroll(self, *_args, **_kwargs):
            return [SimpleNamespace(id=kept), SimpleNamespace(id=stale)], None

        def delete(self, _collection, selector):
            self.deleted.extend(selector.points)

        def close(self):
            pass

    storage.install()
    engine = Index(storage.locations())
    engine.write_executor.submit(lambda: None).result(timeout=2)
    vectors = Vectors()
    engine.vectors = vectors
    engine.vector_baseline = set()
    document = {'id': 'message', 'text': 'durable memory', 'decision': 'index'}
    with closing(engine.connect()) as db, db:
        db.execute(
            'INSERT INTO context_chunks VALUES (?,?,?,?)',
            (kept, 'source', json.dumps(document), json.dumps([0.0] * 384)),
        )
    engine.set_dirty(True)
    try:
        assert engine.repair_vectors()
        assert [point.id for point in vectors.upserts] == [kept]
        assert vectors.deleted == [stale]
        assert engine.status()['index_state'] == 'ready'
    finally:
        engine.close()


def test_index_job_preserves_requested_source_order():
    class DeferredExecutor:
        class Future:
            def result(self):
                return None

        def submit(self, *_args, **_kwargs):
            return self.Future()

        def shutdown(self, **_kwargs):
            pass

    storage.install()
    engine = Index(storage.locations())
    engine.write_executor.shutdown(wait=True)
    engine.write_executor = DeferredExecutor()
    try:
        result = engine.submit(
            {
                "operation": "index",
                "sources": ["/newest.jsonl", "/older.jsonl", "/newest.jsonl"],
            },
            defer_index=True,
        )
        assert result["_dispatch_job"] == result["job_id"]
        with closing(engine.connect()) as db:
            row = db.execute(
                "SELECT sources FROM ingest_jobs WHERE id=?", (result["job_id"],)
            ).fetchone()
        assert json.loads(row[0]) == ["/newest.jsonl", "/older.jsonl"]
    finally:
        engine.close()


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST') != '1', reason='Explicit real FastEmbed integration test')
def test_real_index_lifecycle(tmp_path, install_embedding):
    storage.install()
    paths = storage.locations()
    install_embedding()
    a, b = tmp_path/'a.jsonl', tmp_path/'b.jsonl'
    source(a)
    source(b, '/project/b', 'The web interface uses a purple navigation sidebar.', '2026-09-09')
    before = a.read_bytes()
    engine = Index(paths)
    try:
        # All Qdrant operations run on the same owner thread as the daemon uses.
        first = engine.write_executor.submit(engine.index_file,a).result(timeout=120)
        assert first['messages'] == 2
        engine.write_executor.submit(engine.index_file,b).result(timeout=120)
        assert engine.write_executor.submit(engine.index_file,a).result()['unchanged']
        found = engine.submit({'operation':'search_context','query':'rover electrical power batteries','limit':5,'project':'/project/a'})
        assert found['results'] and all(r['project']=='/project/a' for r in found['results'])
        assert any('lithium' in r['text'] for r in found['results'])
        recent = engine.submit({'operation':'recent_context','since':'2026-09-09','until':'2026-09-10'})
        assert len(recent['results']) == 1
        context = engine.submit({'operation':'get_context','context_id':found['results'][0]['context_id']})
        assert len(context['results']) == 2
        assert a.read_bytes() == before
        # Rewrite removes obsolete searchable text and produces a new checkpoint.
        source(a, text='The rover now uses a hydrogen fuel cell.')
        engine.write_executor.submit(engine.index_file,a).result(timeout=120)
        expanded = engine.submit({'operation':'get_context','context_id':found['results'][0]['context_id']})
        assert 'hydrogen' in expanded['results'][0]['text']
        assert engine.status()['messages'] == 4
        archived = tmp_path/'archived.jsonl'
        a.rename(archived)
        engine.write_executor.submit(engine.index_file,archived).result(timeout=120)
        assert engine.status()['messages'] == 4
        assert len(engine.status()['sources']) == 2
        archived.rename(a)
        engine.write_executor.submit(engine.index_file,a).result(timeout=120)
        # Simulate interrupted vector update; SQL remains authoritative.
        engine.set_dirty(True)
        engine.write_executor.submit(engine.repair_vectors).result()
        assert engine.submit({'operation':'search_context','query':'hydrogen fuel cell'})['results']
    finally:
        engine.close()
    engine = Index(paths)
    try:
        assert engine.submit({'operation':'recent_context'})['results']
    finally:
        engine.close()
    unknown = paths['data']/'vectors'/'user-note'
    unknown.write_text('keep')
    storage.uninstall(purge=True)
    assert unknown.read_text() == 'keep'
    assert a.exists() and b.exists()
    assert not storage.database_path(paths).exists()


def test_long_context_can_be_read_without_losing_tail(tmp_path):
    storage.install()
    engine = Index(storage.locations())
    try:
        document = {'text':'a'*25000+'THE END', 'role':'user'}
        with closing(engine.connect()) as db, db:
            db.execute('INSERT INTO context_messages VALUES (?,?,?,?,?,?,?)', ('m','source','turn',1,None,None,json.dumps(document)))
        first = engine.submit({'operation':'get_context','context_id':'turn','limit':1})['results'][0]
        assert first['text_truncated'] and first['next_char_offset'] == 24000
        last = engine.submit({'operation':'get_context','context_id':'turn','limit':1,'char_offset':24000})['results'][0]
        assert last['text'].endswith('THE END') and not last['text_truncated']
    finally:
        engine.close()


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST') != '1', reason='Explicit real daemon/embedding integration')
def test_daemon_replays_durable_jobs_and_serves_cli(tmp_path, install_embedding):
    storage.install()
    paths = storage.locations()
    install_embedding()
    transcript = tmp_path/'source.jsonl'
    source(transcript)
    original = transcript.read_bytes()
    engine = Index(paths)
    with closing(engine.connect()) as db, db:
        db.execute('INSERT INTO ingest_jobs VALUES (?,?,?,?,?,?)', ('resume-me','running',json.dumps([str(transcript)]),'before','before','{}'))
    engine.close()
    command = [sys.executable,'-m','bl_context.daemon','--installation-id',storage.read_manifest(paths)['installation_id']]
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    cli = str(Path(sys.executable).parent/'blctx')
    def invoke(*args):
        proc = subprocess.run([cli,*args],text=True,capture_output=True,timeout=45)
        assert proc.returncode == 0, proc.stdout+proc.stderr
        return json.loads(proc.stdout)
    try:
        deadline = time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                identity(paths)
                break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail('Daemon did not start')
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            job = invoke('index-status','resume-me')
            if job['state'] not in ('running','queued'):
                break
            time.sleep(.1)
        assert job['state'] == 'complete', job
        assert invoke('search','battery energy supply')['results']
        assert invoke('recent','--since','2026-09-08','--until','2026-09-09')['results']
        result = invoke('index',str(transcript),'--wait')
        assert result['result']['files'][0]['unchanged']
        assert transcript.read_bytes() == original
    finally:
        child.terminate()
        _, errors = child.communicate(timeout=15)
        assert child.returncode == 0, errors.decode()
    assert not (paths['state']/'daemon.sock').exists()
