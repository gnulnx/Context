"""Real inventories, declared snapshot imports and recovery; no source mutation."""
from contextlib import closing
import json
import os
from pathlib import Path
import time
import uuid

import pytest
from click.testing import CliRunner

from bl_context import discovery, history, storage
from bl_context.cli import main
from bl_context.index import Index
from test_index import source


def corpus(tmp_path):
    root = Path(os.environ['CODEX_HOME'])
    (root/'sessions').mkdir(parents=True,exist_ok=True)
    (root/'archived_sessions').mkdir(exist_ok=True)
    a,b = root/'sessions/a.jsonl',root/'archived_sessions/b.jsonl'
    source(a,project='/project/rover')
    source(b,project='/project/web',text='The website uses a purple sidebar.',day='2026-09-09')
    return a,b


def test_discovery_lifecycle_freshness_and_no_history(tmp_path):
    a,b=corpus(tmp_path)
    before={p:p.read_bytes() for p in (a,b)}
    runner=CliRunner()
    def call(args,code):
        result=runner.invoke(main,args+['--json'])
        assert result.exit_code==code,result.output
        return json.loads(result.output)
    call(['status','--step','session_discovery'],1)
    call(['install','codex','--step','session_discovery'],0)
    summary,rows=discovery.inventory()
    assert (summary['sessions'],summary['active'],summary['archived'])==(2,1,1)
    assert {r['project'] for r in rows}=={'/project/rover','/project/web'}
    assert all(r['first_timestamp'] and r['last_timestamp'] and r['digest'] for r in rows)
    call(['install','codex','--step','session_discovery'],0)
    assert discovery.inventory()[0]['id']==summary['id']
    with a.open('a') as stream:stream.write(json.dumps({'type':'event_msg','payload':{'type':'token_count'}})+'\n')
    assert discovery.freshness(summary,rows)['appended_files']==1
    call(['install','codex','--step','session_discovery'],0)
    assert discovery.inventory()[0]['id']!=summary['id']
    a.write_bytes(before[a])
    call(['doctor','--step','session_discovery'],1)
    call(['uninstall','codex'],0)
    call(['status','--step','session_discovery'],1)
    call(['install','codex','--step','data_directory'],0)
    call(['doctor','--step','session_discovery'],1)
    call(['install','codex','--step','session_discovery'],0)
    assert all(p.read_bytes()==data for p,data in before.items())
    data=call(['install','codex','--no-history'],1)
    assert {r['step_id'] for r in data['checks'] if r['status']=='skipped'}=={'session_discovery','history_index','history_retrieval'}


def test_empty_missing_unsupported_symlink_and_corruption(tmp_path):
    storage.install()
    with pytest.raises(RuntimeError,match='root is missing'):discovery.install()
    root=Path(os.environ['CODEX_HOME']);root.mkdir()
    discovery.install()
    assert discovery.inventory()[0]['empty']
    assert 'Empty history' in discovery.verify()
    (root/'sessions').mkdir()
    bad=root/'sessions/bad.jsonl';bad.write_text('{truncated')
    discovery.install()
    summary,rows=discovery.inventory()
    assert summary['unsupported']==1 and summary['counts']['unclassified']==1
    with closing(discovery.connect(storage.locations())) as db,db:
        db.execute('DELETE FROM discovery_sources')
    with pytest.raises(RuntimeError,match='count'):discovery.verify()
    bad.unlink();bad.symlink_to(tmp_path/'outside.jsonl')
    with pytest.raises(RuntimeError,match='symbolic'):discovery.install()


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST')!='1',reason='Real CPU model and Qdrant')
def test_declared_import_retrieval_partial_incremental_and_restart(tmp_path,install_embedding,monkeypatch):
    a,b=corpus(tmp_path)
    storage.install();install_embedding();discovery.install()
    addition = dict(type='response_item', timestamp='2026-09-10T12:00:00Z',
                    payload=dict(type='message',role='assistant',phase='commentary',
                                 content=[dict(type='output_text',text='The rover charger passed its incremental acceptance test.')]))
    with a.open('a') as stream:stream.write(json.dumps(addition)+'\n')
    paths=storage.locations();engine=Index(paths)
    from bl_context import retrieval_cli
    monkeypatch.setattr(retrieval_cli,'call',engine.submit)
    def wait(job):
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            state=engine.status(job)
            if state['state'] not in ('queued','running'):return state
            time.sleep(.02)
        pytest.fail('Job did not complete')
    try:
        history.install()
        assert '1 appended files' in history.verify()
        assert engine.status()['messages']==4  # Only the declared prefix, despite the new line.
        discovery.install();history.install()
        assert engine.status()['messages']==5
        duplicate=a.parent/'duplicate.jsonl';duplicate.write_bytes(a.read_bytes())
        discovery.install();history.install();assert 'source-linked' in history.verify()
        assert engine.status()['messages']==5  # Same source session and bytes, no duplicate messages.
        request={'operation':'search_context','query':'rover battery power','project':'/project/rover','since':'2026-09-08','until':'2026-09-09'}
        found=engine.submit(request)['results'];assert found and all(r['project']=='/project/rover' for r in found)
        assert engine.submit({'operation':'get_context','context_id':found[0]['context_id'],'project':'/wrong'})['results']==[]
        counts=engine.status()['messages'];history.install();assert engine.status()['messages']==counts
        fork=a.parent/'fork.jsonl'
        forkrows=[json.loads(line) for line in a.read_text().splitlines()]
        forkrows[0]['payload'].update(id='fork',forked_from_id='a')
        fork.write_text('\n'.join(map(json.dumps,forkrows))+'\n')
        with b.open('a') as stream:stream.write('{truncated')
        discovery.install();history.install()
        with pytest.raises(RuntimeError,match='partial'):history.verify()
        b.write_bytes(b.read_bytes()[:-len('{truncated')])
        discovery.install();history.install();assert 'source-linked' in history.verify()
        forkdocs=engine.submit({'operation':'search_context','query':'lithium batteries','limit':50})['results']
        assert any(r['source_session_id']=='fork' and r['parent_source_session_id']=='a' for r in forkdocs)
        # An interrupted batch is durable even before the source checkpoint commits.
        long=a.parent/'long.jsonl';source(long,text=' '.join(f'battery engineering measurement {n}.' for n in range(12000)))
        callbacks=[]
        def interrupt(stage,done,total):
            callbacks.append((done,total))
            if done>=32:engine.stopping.set()
        with pytest.raises(InterruptedError):
            engine.executor.submit(engine.index_file,long,progress=interrupt).result()
        with closing(engine.connect()) as db:
            assert db.execute('SELECT count(*) FROM embedding_checkpoints').fetchone()[0]>=32
        engine.close();engine=Index(paths);monkeypatch.setattr(retrieval_cli,'call',engine.submit)
        model=engine.load_model();original=model.passage_embed;embedded=[]
        def count(texts,*args,**kwargs):
            values=list(texts);embedded.extend(values);return original(values,*args,**kwargs)
        monkeypatch.setattr(model,'passage_embed',count)
        result=engine.executor.submit(engine.index_file,long).result(timeout=90)
        assert len(embedded)<result['chunks']
        assert callbacks[-1][0]>=32
        discovery.install();history.install();assert 'source-linked' in history.verify()
        # No retained inventory can revive indexing after disconnection.
        storage.uninstall()
        with pytest.raises(RuntimeError):engine.executor.submit(engine.index_file,a).result()
        with pytest.raises(RuntimeError):history.verify()
    finally:
        engine.close()


def test_streaming_classifier_excludes_credentials_and_oversized_records(tmp_path):
    from bl_context.preview import classify_transcript,MAX_RECORD_BYTES
    p=tmp_path/'private.jsonl';source(p,text='api_key=THIS_IS_A_SECRET_VALUE_NEVER_INDEX')
    with p.open('a') as stream:
        stream.write(json.dumps({'type':'response_item','payload':{'type':'reasoning','text':'PRIVATE THOUGHT'}})+'\n')
        stream.write('x'*(MAX_RECORD_BYTES+100)+'\n')
    turns=[];data=classify_transcript(p,on_turn=turns.append)
    assert not data['turns']
    assert data['counts']['unclassified']==1
    assert not any('SECRET' in r.get('text','') or 'THOUGHT' in r.get('text','') for t in turns for r in t['records'])


def test_inventory_pagination_and_repair(tmp_path):
    corpus(tmp_path);storage.install();discovery.install()
    result=CliRunner().invoke(main,['sessions','--limit','1'])
    data=json.loads(result.output)
    assert result.exit_code==0 and data['has_more'] and data['next_offset']==1
    assert len(data['files'])==1 and data['inventory']['files']==2
    with closing(discovery.connect(storage.locations())) as db,db:
        db.execute('DELETE FROM discovery_runs')
    with pytest.raises(RuntimeError,match='missing'):discovery.verify()
    discovery.install()
    assert discovery.inventory()[0]['files']==2


def test_terminal_progress_uses_file_and_chunk_counts(monkeypatch):
    monkeypatch.setenv('TERM','xterm-256color')
    from io import StringIO
    from rich.console import Console
    stream=StringIO()
    with history.progress(Console(file=stream, force_terminal=True, width=100)):
        history.emit('Discovering sessions',0,2)
        history.emit('Discovery complete',2,2)
        history.emit('Embedding current session',0,2,32,64)
        history.emit('Import complete',2,2)
    output=stream.getvalue()
    assert 'Current session chunks' in output and '32/64' in output and 'Import complete' in output


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST')!='1',reason='Real daemon interruption and CPU model')
def test_daemon_killed_mid_import_recovers_declared_scope(tmp_path,install_embedding):
    import subprocess
    import sys
    from bl_context.retrieval_cli import call
    from bl_context.service import identity
    a,b=corpus(tmp_path)
    source(a,project='/project/rover',text=' '.join(f'rover battery measurement {n}.' for n in range(15000)))
    original=a.read_bytes()
    storage.install();install_embedding();discovery.install()
    paths=storage.locations();identifier=storage.read_manifest(paths)['installation_id']
    command=[sys.executable,'-m','bl_context.daemon','--installation-id',identifier]
    child=None
    def wait(check,seconds=90):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            try:
                value=check()
                if value:return value
            except OSError:pass
            time.sleep(.1)
        pytest.fail('Daemon/import did not reach expected state')
    try:
        child=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        wait(lambda:identity(paths),15)
        job=call({'operation':'index_inventory','inventory_id':discovery.inventory()[0]['id']})['job_id']
        wait(lambda:call({'operation':'index_status','job_id':job})['result'].get('chunks_completed',0)>=32)
        child.kill();child.wait(timeout=10)
        with closing(discovery.connect(paths,readonly=True)) as db:
            assert db.execute('SELECT count(*) FROM embedding_checkpoints').fetchone()[0]>=32
            assert db.execute('SELECT state FROM ingest_jobs WHERE id=?',(job,)).fetchone()[0]=='running'
        child=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        wait(lambda:identity(paths),15)
        wait(lambda:call({'operation':'index_status','job_id':job})['state']=='complete')
        assert 'source-linked' in history.verify()
        assert a.read_bytes()==original
    finally:
        if child and child.poll() is None:
            child.terminate();child.wait(timeout=20)
