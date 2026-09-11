"""Real embedding/retrieval acceptance runs explicitly; no network in normal CI."""
import json
import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from bl_context import storage
from bl_context.index import Index, epoch
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
        first = engine.executor.submit(engine.index_file,a).result(timeout=120)
        assert first['messages'] == 2
        engine.executor.submit(engine.index_file,b).result(timeout=120)
        assert engine.executor.submit(engine.index_file,a).result()['unchanged']
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
        engine.executor.submit(engine.index_file,a).result(timeout=120)
        expanded = engine.submit({'operation':'get_context','context_id':found['results'][0]['context_id']})
        assert 'hydrogen' in expanded['results'][0]['text']
        assert engine.status()['messages'] == 4
        archived = tmp_path/'archived.jsonl'
        a.rename(archived)
        engine.executor.submit(engine.index_file,archived).result(timeout=120)
        assert engine.status()['messages'] == 4
        assert len(engine.status()['sources']) == 2
        archived.rename(a)
        engine.executor.submit(engine.index_file,a).result(timeout=120)
        # Simulate interrupted vector update; SQL remains authoritative.
        engine.set_dirty(True)
        engine.executor.submit(engine.open_vectors).result()
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
