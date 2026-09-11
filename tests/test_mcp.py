import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from bl_context import mcp_registration, service, storage
from bl_context.index import Index
from bl_context.service import identity


def test_sessions_and_durable_updates(monkeypatch):
    storage.install()
    # Exercise a real unavailable embedding backend; durable notes must survive it.
    monkeypatch.setattr(Index, 'load_model', lambda self: (_ for _ in ()).throw(RuntimeError('offline')))
    engine = Index(storage.locations())
    binding = str(uuid.uuid4())
    request = dict(operation='open_session', binding_key=binding, project='/example')
    try:
        session = engine.submit(request)['session_id']
        assert session != binding
        assert engine.submit(request)['session_id'] == session
        with pytest.raises(ValueError, match='metadata'):
            engine.submit({**request, 'project':'/other'})
        child = engine.submit({**request, 'binding_key':str(uuid.uuid4()), 'parent_session_id':session})
        assert child['session_id'] != session
        note = dict(operation='log_update', session_id=session, update_id=str(uuid.uuid4()), text='Battery test passed', tags=['power','test'], authorship='user_requested')
        assert engine.submit(note)['stored']
        assert engine.submit(note)['reused']
        with pytest.raises(ValueError, match='different content'):
            engine.submit({**note, 'text':'Changed'})
        docs = engine.submit(dict(operation='get_context', context_id=note['update_id']))['results']
        assert len(docs) == 1 and docs[0]['source_type'] == 'authored_update'
        assert docs[0]['authorship'] == 'user_requested'
        recalled = engine.submit(
            {'operation': 'search_context', 'query': 'battery test'}
        )['results']
        assert recalled[0]['text'] == 'Battery test passed'
        assert recalled[0]['project'] == '/example'
        assert recalled[0]['retrieval_mode'] == 'lexical'
        assert engine.submit(
            {
                'operation': 'search_context',
                'query': 'battery test',
                'project': '/other',
            }
        )['results'] == []
        assert engine.status()['authored_updates_pending'] == 1
        assert engine.status()['index_state'] == 'syncing'
    finally:
        engine.close()
    engine = Index(storage.locations())
    monkeypatch.setattr(engine, 'load_model', lambda: (_ for _ in ()).throw(RuntimeError('offline')))
    try:
        assert engine.submit(request)['session_id'] == session
        assert engine.submit(dict(operation='recent_context'))['results']
    finally:
        engine.close()


@pytest.mark.skipif(shutil.which('codex') is None, reason='Codex CLI required')
def test_registration_preserves_config_and_handles_collisions(tmp_path):
    storage.install()
    root = mcp_registration.root()
    root.mkdir(parents=True)
    path = root/'config.toml'
    path.write_text('model = "example"\n[mcp_servers.unrelated]\ncommand = "/bin/true"\n')
    original = mcp_registration.config(root)
    mcp_registration.install()
    mcp_registration.install()
    assert mcp_registration.verify()
    assert mcp_registration.unrelated(mcp_registration.config(root)) == original
    mcp_registration.uninstall()
    assert mcp_registration.config(root) == original
    path.write_text(path.read_text()+'\n[mcp_servers.base-layer-context]\ncommand = "/bin/false"\n')
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='collision'):
        mcp_registration.install()
    assert path.read_bytes() == before


def test_stdio_tools_and_daemon_failure(tmp_path):
    storage.install()
    identifier = storage.read_manifest(storage.locations())['installation_id']
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=['-m','bl_context.mcp_server','--installation-id',identifier], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listing = await client.list_tools()
                assert {t.name for t in listing.tools} == {'recent_context','search_context','get_context','context_status','open_session','log_update'}
                failed = await client.call_tool('context_status', {})
                assert failed.isError
    asyncio.run(exercise())


REAL_BUS = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
REAL_RUNTIME = os.environ.get('XDG_RUNTIME_DIR')


@pytest.mark.skipif(os.environ.get('BLCTX_SYSTEMD_TEST') != '1', reason='Explicit systemd/Codex acceptance')
def test_mcp_install_lifecycle(tmp_path, monkeypatch):
    for key, value in [('DBUS_SESSION_BUS_ADDRESS', REAL_BUS), ('XDG_RUNTIME_DIR', REAL_RUNTIME)]:
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    root = mcp_registration.root()
    root.mkdir(parents=True)
    (root/'config.toml').write_text('[mcp_servers.unrelated]\ncommand = "/bin/true"\n')
    original = mcp_registration.config(root)
    transcript = root/'original.jsonl'
    transcript.write_text('preserve original')
    cli = str(Path(sys.executable).parent/'blctx')
    evidence = []
    def run(args, expected):
        result = subprocess.run([cli, *args, '--json'], capture_output=True, text=True, timeout=45)
        assert result.returncode == expected, result.stdout+result.stderr
        evidence.append({'command':['blctx',*args,'--json'], 'exit_code':result.returncode,'json':json.loads(result.stdout)})
        assert transcript.read_text() == 'preserve original'
        assert mcp_registration.unrelated(mcp_registration.config(root)) == original
    try:
        run(['status','--step','codex_mcp'], 1)
        for _ in range(2):
            run(['install','codex','--step','codex_mcp'], 0)
            run(['install','codex','--step','codex_mcp'], 0)
            run(['status','--step','codex_mcp'], 0)
            run(['doctor','--step','codex_mcp'], 0)
            run(['uninstall','codex'], 0)
            run(['status','--step','codex_mcp'], 1)
            run(['doctor','--step','codex_mcp'], 1)
        if os.environ.get('BLCTX_MCP_EVIDENCE'):
            Path(os.environ['BLCTX_MCP_EVIDENCE']).write_text(json.dumps(evidence,indent=2)+'\n')
    finally:
        mcp_registration.uninstall()
        service.uninstall()
        storage.uninstall(purge=True)


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST') != '1', reason='Real model and MCP acceptance')
def test_live_session_updates_over_stdio(tmp_path, install_embedding):
    storage.install()
    paths = storage.locations()
    install_embedding()
    identifier = storage.read_manifest(paths)['installation_id']
    child = subprocess.Popen([sys.executable,'-m','bl_context.daemon','--installation-id',identifier], stderr=subprocess.PIPE)
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=['-m','bl_context.mcp_server','--installation-id',identifier], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                async def call(name, args):
                    response = await client.call_tool(name, args)
                    assert not response.isError, response
                    return response.structuredContent
                args = {'binding_key':str(uuid.uuid4()), 'project':'/mcp-test'}
                session = (await call('open_session',args))['session_id']
                assert (await call('open_session',args))['session_id'] == session
                note = dict(session_id=session,update_id=str(uuid.uuid4()),text='We selected titanium panels for the lunar rover.',tags=['rover','materials'])
                logged = await call('log_update',note)
                assert logged['stored']
                assert (await call('log_update',note))['reused']
                result = await call('search_context',dict(query='lunar vehicle titanium material',project='/mcp-test'))
                assert result['results'][0]['source_type'] == 'authored_update'
                assert result['results'][0]['tags'] == ['materials','rover']
                assert (await call('get_context',dict(context_id=logged['context_id'])))['results'][0]['text'] == note['text']
                assert (await call('recent_context',{}))['results']
                deadline = time.monotonic() + 15
                while True:
                    status = await call('context_status',{})
                    if status['authored_updates_pending'] == 0:
                        break
                    assert status['index_state'] == 'syncing'
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.1)
                assert status['index_state'] == 'ready'
    try:
        deadline = time.monotonic()+15
        while True:
            try:
                identity(paths)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.1)
        asyncio.run(exercise())
    finally:
        child.terminate()
        _, errors = child.communicate(timeout=15)
        assert child.returncode == 0, errors.decode()
