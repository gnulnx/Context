"""Product acceptance: global evidence, durable handoffs, bounded MCP, retention."""

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bl_context import (
    checks,
    codex_skills,
    discovery,
    mcp_registration,
    retrieval,
    storage,
)
from bl_context.index import SELECTION, Index, epoch
from bl_context.installers import CodexInstallerAdapter
from bl_context.service import identity

REAL_BUS = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
REAL_RUNTIME = os.environ.get('XDG_RUNTIME_DIR')


def stamp(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def engine(monkeypatch):
    storage.install()
    index = Index(storage.locations())
    index.write_executor.submit(lambda: None).result(timeout=10)
    monkeypatch.setattr(index, 'load_model', lambda: (_ for _ in ()).throw(RuntimeError('offline test')))
    yield index
    index.close()


def seed(engine, text, *, project='/work/context', agent='codex', days=0,
         source_type='conversation', session=None, context=None, tags=None, phase='final_answer'):
    identifier = str(uuid.uuid4())
    timestamp = stamp(days)
    document = dict(id=identifier, context_id=context or identifier,
                    source_session_id=session or str(uuid.uuid4()), source_type=source_type,
                    project=project, agent=agent, timestamp=timestamp, timestamp_epoch=epoch(timestamp),
                    text=text, phase=phase, role='assistant', decision='index', tags=tags or [],
                    source={'path': '/original/conversation.jsonl', 'line': 12})
    with closing(engine.connect()) as db, db:
        db.execute('INSERT INTO context_messages VALUES (?,?,?,?,?,?,?)',
                   (identifier, '/original/conversation.jsonl', document['context_id'],
                    1, epoch(timestamp), project, json.dumps(document)))
    return identifier


def save(engine, text, *, project='/work/context', agent='codex', tags=None):
    session = engine.submit(dict(operation='open_session', binding_key=str(uuid.uuid4()), project=project, agent=agent))
    return engine.submit(dict(operation='log_update', session_id=session['session_id'],
                              update_id=str(uuid.uuid4()), text=text, tags=tags or [],
                              kind='note', authorship='user_requested'))


def assert_bounded(result):
    assert retrieval.size(result) <= retrieval.RESPONSE_BYTES
    assert 'sources' not in result.get('coverage', {})
    assert 'jobs' not in result.get('coverage', {})


def test_a_recent_projects_diverse_outcomes_and_pagination(engine):
    noisy_session = str(uuid.uuid4())
    for _ in range(240):
        seed(engine, 'Still working through the same build output.', project='/work/noisy', session=noisy_session)
    seed(engine, 'Shipped the ordering page.', project='/work/cad', days=1)
    save(engine, 'Installer works on macOS; next validate a clean install.', project='/work/context')
    save(engine, 'LiDAR replay qualification passed; robot motion remains unqualified.', project='/work/robot')
    result = engine.submit(dict(operation='work_overview'))
    assert {item['project'] for item in result['results']} == {'/work/noisy', '/work/cad', '/work/context', '/work/robot'}
    noisy = next(item for item in result['results'] if item['project'] == '/work/noisy')
    assert len(noisy['items']) == 1
    assert noisy['items_omitted'] == 239
    assert result['coverage_incomplete']  # Representative, not exhaustive.
    assert_bounded(result)
    first = engine.submit(dict(operation='work_overview', limit=2))
    second = engine.submit(dict(operation='work_overview', limit=2, offset=first['next_offset']))
    assert first['omitted_projects'] == 2 and not second['has_more']
    assert len({item['project'] for page in (first, second) for item in page['results']}) == 4


@pytest.mark.parametrize('subject,question,old,new', [
    ('magic word', 'What is the most recent magic word?', 'Alpha', 'Beta'),
    ('deployment target', 'Which deployment target are we currently using?', 'Atlas', 'EssenceOS'),
    ('release channel', 'What is our current release channel?', 'canary', 'stable'),
])
def test_b_newest_fact_across_sessions_without_special_case(engine, subject, question, old, new):
    save(engine, f'Our {subject} is {old}.')
    assert old in engine.submit(dict(operation='search_context', query=question))['results'][0]['text']
    latest = save(engine, f'Our {subject} is {new}.', project='/another/project', agent='claude')
    response = engine.submit(dict(operation='search_context', query=question))
    assert response['results'][0]['id'] == latest['update_id']
    assert new in response['results'][0]['text']
    assert response['results'][0]['agent'] == 'claude'


def test_c_factual_evidence_near_matches_not_a_timeline(engine):
    for _ in range(60):
        seed(engine, 'We tested the installer and its CLI output.', project='/work/context')
    target = seed(engine, 'Background material. ' * 300 + 'B2 uses a Unitree L2 LiDAR sensor. ' + 'Appendix. ' * 200,
                  project='/work/robot', days=2)
    response = engine.submit(dict(operation='search_context', query='What kind of LiDAR is the B2 using?'))
    assert response['results'][0]['id'] == target
    assert 'Unitree L2' in response['results'][0]['text']
    assert response['results'][0]['text_char_start'] > 0
    assert len(response['results']) == 1
    assert_bounded(response)


def test_d_exact_tag_handoff_and_f_mixed_provenance(engine):
    for agent, project in [('codex', 'context'), ('claude', 'robot'), ('gemini', 'cad')]:
        save(engine, f'{project}: transport tests passed. Next verify replay. Artifact: /work/{project}/report.json.',
             project=f'/work/{project}', agent=agent, tags=['lidar-debug'])
    save(engine, 'This text mentions lidar-debug but has no tag.')
    save(engine, 'A different tag.', tags=['lidar-debug-more'])
    response = engine.submit(dict(operation='get_tag', tag='lidar-debug'))
    assert len(response['results']) == 3
    assert {item['agent'] for item in response['results']} == {'codex', 'claude', 'gemini'}
    assert all('Next verify replay' in item['text'] for item in response['results'])
    assert engine.submit(dict(operation='get_tag', tag='LIDAR-DEBUG'))['results'] == []
    assert_bounded(response)


def test_capture_binding_can_gain_agent_provenance_without_changing_identity(engine):
    request = dict(operation='open_session', binding_key=str(uuid.uuid4()), project='/work/robot')
    original = engine.submit(request)
    annotated = engine.submit({**request, 'agent': 'codex'})
    assert annotated['session_id'] == original['session_id'] and annotated['reused']
    assert engine.submit(request)['session_id'] == original['session_id']
    with pytest.raises(ValueError, match='different metadata'):
        engine.submit({**request, 'agent': 'claude'})


def test_e_large_history_complete_object_budget_and_continuation(engine):
    contexts = []
    for number in range(55):
        contexts.append(seed(engine, f'Outcome {number}: ' + '\U0001f916\\"\n' * 8000,
                             project=f'/work/project-{number}'))
    with closing(engine.connect()) as db, db:
        for number in range(100):
            report = dict(selection=SELECTION, unclassified=1, large='diagnostic' * 16000)
            db.execute('INSERT INTO indexed_sources VALUES (?,?,?,?,?)',
                       (f'/missing/{number}', 'digest', 42, stamp(), json.dumps(report)))
        db.execute('INSERT INTO ingest_jobs VALUES (?,?,?,?,?,?)',
                   ('job', 'complete', '[]', stamp(), stamp(), json.dumps({'large': 'x' * 1000000})))
    for operation in ('work_overview', 'recent_context', 'search_context'):
        response = engine.submit(dict(operation=operation, query='Outcome', limit=50))
        assert_bounded(response)
        assert response['has_more'] and response['next_offset'] == len(response['results'])
        assert response['response_truncated'] and response['coverage_incomplete']
        assert response['coverage']['missing_sources'] == 100
        assert response['coverage']['partial_sources'] == 100
        for item in response['results']:
            assert all(message['text'] for message in item.get('messages', item.get('items', [item])))
    status = engine.submit(dict(operation='index_status'))
    assert retrieval.size(status) < 3000
    # Expansion obeys bytes even for escaped/Unicode text and reconstructs exactly.
    identifier = contexts[0]
    offset, parts = 0, []
    while True:
        response = engine.submit(dict(operation='get_context', context_id=identifier, limit=1, char_offset=offset))
        assert_bounded(response)
        message = response['results'][0]
        parts.append(message['text'])
        offset = message['next_char_offset']
        if offset is None:
            break
    assert ''.join(parts) == 'Outcome 0: ' + '\U0001f916\\"\n' * 8000


def test_retention_covers_all_agents_and_preserves_durable_memory(engine):
    expired = []
    for agent in ('codex', 'claude', 'gemini'):
        expired.append(seed(engine, 'Old ephemeral calibration.', agent=agent, days=8))
        seed(engine, 'Recent calibration outcome.', agent=agent, days=1)
    durable = seed(engine, 'Durable calibration decision.', source_type='authored_update', days=30, tags=['keep'])
    with closing(engine.connect()) as db, db:
        for identifier in expired:
            doc = json.loads(db.execute('SELECT document FROM context_messages WHERE id=?', (identifier,)).fetchone()[0])
            db.execute('INSERT INTO context_chunks VALUES (?,?,?,?)', (str(uuid.uuid4()), 'transcript', json.dumps(doc), '[]'))
    engine.set_dirty(True)
    before = engine.submit(dict(operation='search_context', query='calibration'))
    assert not {item['id'] for item in before['results']} & set(expired)
    assert engine.write_executor.submit(engine.prune).result() == 3
    with closing(engine.connect()) as db:
        assert db.execute('SELECT count(*) FROM context_chunks').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM context_messages').fetchone()[0] == 4
    assert engine.submit(dict(operation='get_tag', tag='keep'))['results'][0]['id'] == durable
    assert engine.submit(dict(operation='get_context', context_id=expired[0]))['results'] == []


def test_real_daemon_and_fresh_stdio_clients_share_memory(tmp_path):
    storage.install()
    paths = storage.locations()
    identifier = storage.read_manifest(paths)['installation_id']
    child = subprocess.Popen([sys.executable, '-m', 'bl_context.daemon', '--installation-id', identifier], stderr=subprocess.PIPE)

    async def fresh_client(action):
        params = StdioServerParameters(command=sys.executable,
                                       args=['-m', 'bl_context.mcp_server', '--installation-id', identifier],
                                       env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()

                async def call(name, arguments):
                    result = await client.call_tool(name, arguments)
                    assert not result.isError, result
                    assert result.structuredContent is None
                    assert len(result.content) == 1
                    assert len(result.model_dump_json().encode()) < 48000
                    payload = json.loads(result.content[0].text)
                    assert_bounded(payload)
                    return payload

                return await action(call)

    async def write(call, value, agent):
        session = await call('open_session', dict(binding_key=str(uuid.uuid4()), project=f'/work/{agent}', agent=agent))
        return await call('log_update', dict(session_id=session['session_id'], update_id=str(uuid.uuid4()),
                                            text=f'Our release channel is {value}. Validation passed; next test the installer.',
                                            kind='note', authorship='user_requested', tags=['release-handoff']))

    async def exercise():
        await fresh_client(lambda call: write(call, 'Alpha', 'codex'))
        first = await fresh_client(lambda call: call('search_context', {'query': 'What is our current release channel?'}))
        assert 'Alpha' in first['results'][0]['text']
        await fresh_client(lambda call: write(call, 'Beta', 'claude'))
        latest = await fresh_client(lambda call: call('search_context', {'query': 'What is our current release channel?'}))
        assert 'Beta' in latest['results'][0]['text']
        handoff = await fresh_client(lambda call: call('get_tag', {'tag': 'release-handoff'}))
        assert 'Beta' in handoff['results'][0]['text'] and 'next test the installer' in handoff['results'][0]['text']
        overview = await fresh_client(lambda call: call('work_overview', {}))
        assert {item['project'] for item in overview['results']} == {'/work/codex', '/work/claude'}

        async def large_result(call):
            session = await call('open_session', dict(binding_key=str(uuid.uuid4())))
            logged = await call('log_update', dict(session_id=session['session_id'], update_id=str(uuid.uuid4()),
                                                  text='\U0001f916\\"\n' * 3900))
            result = await call('get_context', dict(context_id=logged['context_id'], limit=1))
            assert result['response_truncated']
            assert result['results'][0]['next_char_offset'] > 0
        await fresh_client(large_result)

    try:
        deadline = time.monotonic() + 15
        while True:
            assert child.poll() is None
            try:
                identity(paths)
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(.05)
        asyncio.run(exercise())
    finally:
        child.terminate()
        _, errors = child.communicate(timeout=15)
        assert child.returncode == 0, errors.decode()


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST') != '1', reason='Real CPU model/vector retention acceptance')
def test_real_vector_retention_and_reimport(tmp_path, install_embedding, monkeypatch):
    storage.install()
    install_embedding()
    path = tmp_path / 'conversation.jsonl'
    rows = [dict(type='session_meta', payload=dict(id=str(uuid.uuid4()), cwd='/work/robot')),
            dict(type='response_item', timestamp=stamp(), payload=dict(type='message', role='user',
                 content=[dict(type='input_text', text='B2 uses a Unitree L2 LiDAR.')])),
            dict(type='response_item', payload=dict(type='message', role='user',
                 content=[dict(type='input_text', text='An undated conversation note.')]))]
    path.write_text('\n'.join(map(json.dumps, rows)) + '\n')
    original = path.read_bytes()
    index = Index(storage.locations())
    try:
        index.write_executor.submit(index.index_file, path).result(timeout=120)
        saved = save(index, 'The deployment target is EssenceOS.', tags=['durable'])
        index.write_executor.submit(lambda: None).result(timeout=120)
        assert index.submit(dict(operation='search_context', query='B2 LiDAR'))['results']
        monkeypatch.setattr(index, 'retention_cutoff', lambda: time.time() + 1)
        assert index.write_executor.submit(index.prune).result(timeout=30) == 2
        index.write_executor.submit(index.repair_vectors).result(timeout=30)
        # Reimporting unchanged or appended old history must not resurrect it.
        index.write_executor.submit(index.index_file, path).result(timeout=30)
        path.write_text(path.read_text() + json.dumps(dict(type='event_msg', payload=dict(type='task_complete'))) + '\n')
        os.utime(path, (time.time() + 86400, time.time() + 86400))
        index.write_executor.submit(index.index_file, path).result(timeout=30)
        with closing(index.connect()) as db:
            assert db.execute('SELECT count(*) FROM context_messages').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM vector_deletes').fetchone()[0] == 0
        points, _ = index.open_vectors().scroll('context_v1', limit=100, with_payload=True)
        assert points and {point.payload['id'] for point in points} == {saved['update_id']}
        assert path.read_bytes().startswith(original)
        assert index.submit(dict(operation='get_tag', tag='durable'))['results']
    finally:
        index.close()


@pytest.mark.skipif(os.environ.get('BLCTX_SYSTEMD_TEST') != '1' or os.environ.get('BLCTX_INDEX_TEST') != '1',
                    reason='Explicit real Codex/systemd/model installation acceptance')
@pytest.mark.parametrize('history_days', [0, 30])
def test_isolated_product_install_and_recall(tmp_path, install_embedding, monkeypatch, history_days):
    for key, value in [('DBUS_SESSION_BUS_ADDRESS', REAL_BUS), ('XDG_RUNTIME_DIR', REAL_RUNTIME)]:
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    # Preseed only the pinned model bytes so this remains reproducible offline.
    storage.install()
    install_embedding()
    transcript = discovery.codex_root() / 'sessions' / 'sample.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('\n'.join(map(json.dumps, [
        dict(type='session_meta', payload=dict(id=str(uuid.uuid4()), cwd='/work/robot')),
        dict(type='response_item', timestamp=stamp(history_days), payload=dict(type='message', role='user',
             content=[dict(type='input_text', text='The robot uses a Unitree L2 LiDAR.')]))])) + '\n')
    original = transcript.read_bytes()
    adapter = CodexInstallerAdapter()
    try:
        installed = adapter.install(choose_hooks=lambda: True)
        assert checks.is_ready(installed), [result.to_dict() for result in installed]
        assert checks.is_ready(adapter.verify())
        assert 'work_overview' in codex_skills.target().read_text()
        opened = mcp_registration.call_tool('open_session', dict(binding_key=str(uuid.uuid4()), project='/work/new'))
        mcp_registration.call_tool('log_update', dict(session_id=opened['session_id'], update_id=str(uuid.uuid4()),
                                                     text='The deployment target is EssenceOS.', tags=['install-handoff']))
        recalled = mcp_registration.call_tool('search_context', dict(query='What is our deployment target?'))
        assert recalled['results'][0]['text'] == 'The deployment target is EssenceOS.'
        tagged = mcp_registration.call_tool('get_tag', dict(tag='install-handoff'))
        assert tagged['results'][0]['project'] == '/work/new'
        assert transcript.read_bytes() == original
    finally:
        removed = adapter.uninstall()
        assert checks.checks_succeeded(removed), [result.to_dict() for result in removed]
        assert transcript.read_bytes() == original
