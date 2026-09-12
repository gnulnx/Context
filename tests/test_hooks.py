import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

import pytest
from click.testing import CliRunner
from test_index import day_ago, source

from bl_context import capture, codex_hooks, storage
from bl_context.cli import main
from bl_context.codex_probe import query
from bl_context.index import Index


def setup():
    storage.install()
    codex_hooks.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    return paths,manifest['installation_id'],manifest['codex_hooks']['generation']


def event(path=None, runtime='session-a', name='SessionStart', source='startup'):
    return dict(hook_event_name=name,session_id=runtime,cwd='/project',transcript_path=str(path) if path else None,source=source,turn_id='turn1')


def test_binding_duplicates_resume_fork_null_path_and_disconnect(tmp_path):
    paths, identifier, generation = setup()
    def send(data):
        return capture.receive(data,identifier,generation)
    with ThreadPoolExecutor(max_workers=8) as workers:
        outputs = list(workers.map(send,[event()]*8))
    assert all(o == outputs[0] for o in outputs)
    guidance = outputs[0]['hookSpecificOutput']['additionalContext']
    assert 'retrieve global Context before answering; do not guess' in guidance
    assert 'Read-only recall does not require open_session' in guidance
    assert 'Before the first log_update only' in guidance
    assert outputs[0] == send(event(source='resume')) == send(event(source='compact'))
    assert send(event(runtime='fork-b')) != outputs[0]
    assert capture.status(paths)['null_transcripts'] == 2
    with closing(capture.connect(paths)) as db:
        bindings = [r[0] for r in db.execute('SELECT binding_key FROM capture_bindings')]
    assert len(set(bindings)) == 2 and all(uuid.UUID(b) for b in bindings)
    codex_hooks.uninstall()
    assert send(event()) == {}
    assert capture.status(paths)['events'] == {}
    codex_hooks.install()
    assert send(event()) == {}  # Old registration generation cannot enqueue.


def test_hook_waits_out_brief_ownership_contention(monkeypatch):
    paths, identifier, generation = setup()
    attempted = threading.Event()
    original_lock = storage.locked

    @contextmanager
    def observed_lock(paths, timeout=None):
        if timeout is not None:
            attempted.set()
        with original_lock(paths, timeout=timeout):
            yield

    monkeypatch.setattr(storage, 'locked', observed_lock)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with original_lock(paths):
            future = workers.submit(capture.receive, event(), identifier, generation)
            assert attempted.wait(2)
            # Simulate a short filesystem/competing-hook stall beyond the old
            # 200 ms budget. The event must actually be persisted after release.
            time.sleep(.3)
        result = future.result(timeout=2)
    assert result['hookSpecificOutput']['hookEventName'] == 'SessionStart'
    assert capture.status(paths)['events'] == {'SessionStart': 1}


@pytest.mark.parametrize('distinct_sessions', [False, True])
def test_concurrent_hook_processes_persist_bindings(distinct_sessions):
    paths, identifier, generation = setup()
    command = [sys.executable, '-m', 'bl_context.hook_handler',
               '--installation-id', identifier, '--generation', generation,
               '--handler-version', codex_hooks.handler_version()]

    def send(number):
        payload = event(runtime=f'session-{number}' if distinct_sessions else 'session-a')
        result = subprocess.run(command, input=json.dumps(payload), text=True,
                                capture_output=True, timeout=3)
        assert result.returncode == 0 and not result.stderr, result.stderr
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=8) as workers:
        outputs = list(workers.map(send, range(8)))
    assert outputs[0]['hookSpecificOutput']['hookEventName'] == 'SessionStart'
    expected = 8 if distinct_sessions else 1
    assert len({json.dumps(output, sort_keys=True) for output in outputs}) == expected
    assert capture.status(paths)['events'] == {'SessionStart': expected}
    with closing(capture.connect(paths)) as db:
        assert db.execute('SELECT count(*) FROM capture_bindings').fetchone()[0] == expected


def test_hook_imports_only_capture_runtime():
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys; import bl_context.hook_handler; '
         'assert not {"mcp", "rich", "fastembed", "qdrant_client", '
         '"bl_context.codex_hooks", "bl_context.service", "bl_context.embedding"}.intersection(sys.modules)'],
        capture_output=True, text=True, timeout=2,
    )
    assert result.returncode == 0, result.stderr


def test_handler_bounds_and_invalid_input(tmp_path):
    paths, identifier, generation = setup()
    command = [sys.executable,'-m','bl_context.hook_handler','--installation-id',identifier,'--generation',generation,'--handler-version',codex_hooks.handler_version()]
    for payload in ['{bad', 'a'*65537, json.dumps(event()), json.dumps(event(name='PreToolUse'))]:
        start = time.monotonic()
        result = subprocess.run(command,input=payload,text=True,capture_output=True,timeout=2)
        assert result.returncode == 0
        assert isinstance(json.loads(result.stdout),dict)
        assert time.monotonic()-start < 1
    with storage.locked(paths):
        start = time.monotonic()
        result = subprocess.run(command,input=json.dumps(event()),text=True,capture_output=True,timeout=3)
        assert json.loads(result.stdout) == {}
        assert 'capture skipped: Context ownership lock is busy' in result.stderr
        assert time.monotonic()-start < 2  # Registered Codex hook deadline.


def test_hooks_preserve_unrelated_config_and_remove_owned_edits(tmp_path):
    storage.install()
    path = codex_hooks.root()/'hooks.json'
    path.parent.mkdir(parents=True)
    unrelated = {'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo unrelated'}]}]},'description':'User hooks'}
    path.write_text(json.dumps(unrelated))
    codex_hooks.install()
    before = codex_hooks.read(path)
    codex_hooks.install()
    assert codex_hooks.read(path) == before
    doc = codex_hooks.read(path)
    doc['hooks']['Stop'][-1]['hooks'][0]['command'] += ' --user-edit'
    path.write_text(json.dumps(doc))
    codex_hooks.uninstall()
    assert codex_hooks.read(path) == unrelated


def test_uninstall_removes_only_this_installations_modified_hooks():
    storage.install()
    codex_hooks.install()
    path = codex_hooks.root() / 'hooks.json'
    document = codex_hooks.read(path)
    context_group = document['hooks']['Stop'][-1]
    context_group['hooks'][0]['command'] += ' --locally-modified'
    document['hooks']['Stop'].append(
        {
            'hooks': [
                {
                    'type': 'command',
                    'command': (
                        'python -m bl_context.hook_handler '
                        '--installation-id another-installation'
                    ),
                }
            ]
        }
    )
    path.write_text(json.dumps(document))

    codex_hooks.uninstall()

    remaining = json.dumps(codex_hooks.read(path))
    assert '--locally-modified' not in remaining
    assert '--installation-id another-installation' in remaining
    assert not storage.read_manifest(storage.locations()).get('codex_hooks')


def test_skipping_hooks_removes_owned_registration_and_persists_choice():
    storage.install()
    codex_hooks.install()

    codex_hooks.skip()

    assert codex_hooks.skipped()
    assert not codex_hooks.registered()
    assert 'bl_context.hook_handler' not in json.dumps(
        codex_hooks.read(codex_hooks.root() / 'hooks.json')
    )


def test_uninstall_command_removes_current_hooks_without_prompting():
    storage.install()
    codex_hooks.install()
    path = codex_hooks.root() / 'hooks.json'

    result = CliRunner().invoke(main, ['uninstall', 'codex', '--json'])

    assert result.exit_code == 0
    assert 'bl_context.hook_handler' not in json.dumps(codex_hooks.read(path))
    assert not storage.read_manifest(storage.locations()).get('codex_hooks')


def test_fast_ack_while_embedding_worker_is_busy():
    storage.install()
    engine = Index(storage.locations())
    release = threading.Event()
    started = threading.Event()
    def busy():
        started.set()
        release.wait(5)
    engine.write_executor.submit(busy)
    assert started.wait(2)
    try:
        start = time.monotonic()
        session = engine.submit(dict(operation='open_session',binding_key=str(uuid.uuid4())))['session_id']
        note = dict(operation='log_update',session_id=session,update_id=str(uuid.uuid4()),text='Visible status',tags=['status'])
        assert engine.submit(note)['stored']
        assert engine.submit(note)['reused']
        assert time.monotonic()-start < .5
        # Avoid a model download in this failure/queue-boundary test.
        engine.load_model = lambda: (_ for _ in ()).throw(RuntimeError('offline'))
    finally:
        release.set()
        engine.close()


@pytest.mark.skipif(shutil.which('codex') is None, reason='Real Codex hook discovery required')
def test_real_hook_discovery_reports_untrusted_without_bypass(tmp_path):
    setup()
    data = query('hooks/list',codex_hooks.root(),tmp_path)
    hooks = [h for row in data['data'] for h in row['hooks'] if 'bl_context.hook_handler' in h.get('command','')]
    assert len(hooks) == 3
    assert all(h['trustStatus'] == 'untrusted' and h['enabled'] for h in hooks)
    assert {h['eventName'] for h in hooks} == {'sessionStart','stop','sessionEnd'}
    codex_hooks.uninstall()
    data = query('hooks/list',codex_hooks.root(),tmp_path)
    assert not [h for row in data['data'] for h in row['hooks'] if 'bl_context.hook_handler' in h.get('command','')]


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST') != '1', reason='Real background embedding test')
def test_reconciliation_restart_and_visible_update_dedup(tmp_path, install_embedding):
    paths, identifier, generation = setup()
    install_embedding()
    path = tmp_path/'live.jsonl'
    source(path, project='/project')
    progress = 'The rover battery integration now passes its electrical tests.'
    rows = [dict(type='response_item',timestamp=day_ago()+'T12:00:00Z',payload=dict(type='message',role='assistant',phase='commentary',content=[dict(type='output_text',text=progress)])),
            dict(type='response_item',payload=dict(type='reasoning',text='PRIVATE THINKING SENTINEL'))]
    with path.open('a') as stream:
        stream.write('\n'.join(map(json.dumps,rows))+'\n')
    original = path.read_bytes()
    capture.receive(event(path),identifier,generation)
    engine = Index(paths)
    try:
        engine.write_executor.submit(capture.reconcile,engine).result(timeout=120)
        found = engine.submit(dict(operation='search_context',query='rover battery electrical tests'))['results']
        assert any(r['text'] == progress for r in found)
        assert not any('PRIVATE THINKING' in r['text'] for r in found)
        session = next(r['context_session_id'] for r in found if r['text']==progress)
        update = dict(operation='log_update',session_id=session,update_id=str(uuid.uuid4()),text=progress,source_text=progress,tags=['rover','tests'])
        engine.submit(update)
        found = engine.submit(dict(operation='search_context',query='rover battery electrical tests'))['results']
        matches = [r for r in found if r['text']==progress]
        assert len(matches)==1 and matches[0]['source_type']=='authored_update'
        assert matches[0]['source_references'][0]['path']==str(path)
        assert path.read_bytes()==original
    finally:
        engine.close()
    # Crash recovery: persisted Stop event is consumed by a new engine.
    capture.receive(event(path,name='Stop'),identifier,generation)
    engine = Index(paths)
    try:
        engine.write_executor.submit(capture.reconcile,engine).result(timeout=120)
        assert capture.status(paths)['pending']==0
        # A growing transcript is reconciled even without another hook event.
        embedded = []
        model = engine.load_model()
        original_embed = model.passage_embed
        def counted(texts, **kwargs):
            texts = list(texts)
            embedded.extend(texts)
            return original_embed(texts, **kwargs)
        model.passage_embed = counted
        with path.open('a') as stream:
            stream.write(json.dumps(dict(type='response_item',timestamp=day_ago()+'T12:01:00Z',payload=dict(type='message',role='assistant',phase='commentary',content=[dict(type='output_text',text='Final voltage calibration passed.')])) )+'\n')
        engine.write_executor.submit(capture.reconcile,engine).result(timeout=120)
        assert engine.submit(dict(operation='search_context',query='voltage calibration'))['results']
        assert embedded == ['Final voltage calibration passed.']
        assert len([r for r in engine.submit(dict(operation='search_context',query='rover battery electrical tests'))['results'] if r['text']==progress])==1
        codex_hooks.uninstall()
        count=engine.status()['messages']
        with path.open('a') as stream:
            stream.write(json.dumps(rows[0]).replace(progress,'DO NOT CAPTURE AFTER UNINSTALL')+'\n')
        engine.write_executor.submit(capture.reconcile,engine).result(timeout=120)
        assert engine.status()['messages']==count
    finally:
        engine.close()


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST') != '1', reason='Actual daemon/capture/model acceptance')
def test_actual_daemon_drains_hooks_and_recovers_restart(tmp_path, install_embedding):
    paths,identifier,generation = setup()
    install_embedding()
    path = tmp_path/'capture.jsonl'
    source(path)
    capture.receive(event(path),identifier,generation)
    command = [sys.executable,'-m','bl_context.daemon','--installation-id',identifier]
    def wait_until(check):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:
                if check():
                    return
            except OSError:
                pass
            time.sleep(.1)
        pytest.fail('Capture did not complete in time')
    child=subprocess.Popen(command,stderr=subprocess.PIPE)
    try:
        wait_until(lambda:capture.status(paths)['indexed_sessions']==1)
    finally:
        child.terminate()
        _,errors=child.communicate(timeout=10)
        assert child.returncode==0,errors.decode()
    source(path,text='Durable capture survived a daemon restart.')
    capture.receive(event(path,name='Stop'),identifier,generation)
    child=subprocess.Popen(command,stderr=subprocess.PIPE)
    try:
        wait_until(lambda:capture.status(paths)['pending']==0)
        with closing(capture.connect(paths)) as db:
            assert db.execute("SELECT count(*) FROM context_messages WHERE document LIKE '%Durable capture survived%'").fetchone()[0]==1
        codex_hooks.uninstall()
        assert capture.receive(event(path),identifier,generation)=={}
    finally:
        child.terminate()
        _,errors=child.communicate(timeout=10)
        assert child.returncode==0,errors.decode()


def test_codex_hook_state_metadata_is_not_an_inline_definition():
    storage.install()
    path=codex_hooks.root()/'config.toml'
    path.parent.mkdir(parents=True)
    path.write_text('[hooks.state]\n')  # No grant of trust; just the supported metadata container.
    codex_hooks.install()
    codex_hooks.install()
    assert path.read_text()=='[hooks.state]\n'
    codex_hooks.uninstall()
    assert path.read_text()=='[hooks.state]\n'


def test_read_snapshot_does_not_block_hook_or_session_ack():
    paths,identifier,generation=setup()
    engine=Index(paths)
    try:
        with closing(engine.connect()) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM context_chunks').fetchall()
            start=time.monotonic()
            capture.receive(event(),identifier,generation)
            assert engine.submit(dict(operation='open_session',binding_key=str(uuid.uuid4())))['session_id']
            assert time.monotonic()-start < .5
    finally:
        engine.close()
