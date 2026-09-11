import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bl_context import codex_skills as skills
from bl_context import storage


def run_blctx(home, *args):
    env = {**os.environ, 'HOME':str(home), 'CODEX_HOME':str(home/'.codex')}
    return subprocess.run([str(Path(sys.executable).parent/'blctx'), *args, '--json'],
                          env=env, text=True, capture_output=True, timeout=30)


def discover(cwd):
    """Query a fresh real Codex process without a model turn or credentials."""
    skills.root().mkdir(parents=True, exist_ok=True)
    async def query():
        child = await asyncio.create_subprocess_exec('codex','app-server','--stdio',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=None, limit=4*1024*1024, cwd=cwd)
        async def request(identifier, method, params):
            child.stdin.write((json.dumps(dict(id=identifier,method=method,params=params))+'\n').encode())
            await child.stdin.drain()
            while True:
                line = await asyncio.wait_for(child.stdout.readline(), 20)
                assert line, 'Codex closed before discovery response'
                response = json.loads(line)
                if response.get('id') == identifier:
                    assert 'error' not in response, response
                    return response['result']
        try:
            await request(1,'initialize',dict(clientInfo=dict(name='blctx-skill-test',version='1')))
            child.stdin.write(b'{"method":"initialized"}\n')
            return await request(2,'skills/list',dict(cwds=[str(cwd)],forceReload=True))
        finally:
            if child.returncode is None:
                child.terminate()
            await asyncio.wait_for(child.wait(), 10)
    return asyncio.run(query())


def test_codex_skill_red_green_uninstall_red_reinstall(tmp_path):
    sentinel = tmp_path/'transcript.jsonl'
    sentinel.write_text('keep')
    config = tmp_path/'.codex/config.toml'
    config.parent.mkdir()
    config.write_text('# unrelated configuration\n')
    evidence = []
    def run(args, code):
        result = run_blctx(tmp_path,*args)
        assert result.returncode == code, result.stdout+result.stderr
        evidence.append(dict(command=['blctx',*args,'--json'],exit_code=code,json=json.loads(result.stdout)))
        assert sentinel.read_text() == 'keep'
        assert config.read_text() == '# unrelated configuration\n'
    run(['status','--step','codex_skills'],1)
    for _ in range(2):
        run(['install','codex','--step','codex_skills'],0)
        before = skills.target().stat().st_mtime_ns
        run(['install','codex','--step','codex_skills'],0)
        assert skills.target().stat().st_mtime_ns == before
        run(['status','--step','codex_skills'],0)
        run(['doctor','--step','codex_skills'],0)
        run(['uninstall','codex'],0)
        assert not skills.target().exists()
        run(['status','--step','codex_skills'],1)
        run(['doctor','--step','codex_skills'],1)
    if os.environ.get('BLCTX_SKILL_EVIDENCE'):
        Path(os.environ['BLCTX_SKILL_EVIDENCE']).write_text(json.dumps(evidence,indent=2)+'\n')


@pytest.mark.skipif(shutil.which('codex') is None, reason='Actual Codex CLI required for discovery')
@pytest.mark.parametrize('custom_root', [False, True])
def test_fresh_codex_discovers_install_and_removal(tmp_path, monkeypatch, custom_root):
    if custom_root:
        monkeypatch.setenv('CODEX_HOME', str(tmp_path/'custom-codex'))
    storage.install()
    def found():
        response = discover(tmp_path)
        return [s for entry in response['data'] for s in entry['skills'] if s['name'] == skills.SKILL_NAME]
    assert found() == []
    skills.install()
    matches = found()
    assert len(matches) == 1, matches
    assert Path(matches[0]['path']) == skills.target()
    assert matches[0]['enabled']
    skills.uninstall()
    assert found() == []
    skills.install()
    assert len(found()) == 1


@pytest.mark.parametrize('edit', ['content','symlink','hardlink'])
def test_modified_owned_files_preserved(tmp_path, edit):
    storage.install()
    skills.install()
    path = skills.target()
    if edit == 'content':
        path.write_text('user edits')
    elif edit == 'symlink':
        saved = tmp_path/'user-skill'
        path.rename(saved)
        path.symlink_to(saved)
    else:
        os.link(path,tmp_path/'user-skill')
    before = path.read_bytes()
    for operation in (skills.install, skills.verify, skills.uninstall):
        with pytest.raises(RuntimeError):
            operation()
        assert path.read_bytes() == before
    assert 'codex_skill' in storage.read_manifest(storage.locations())


def test_upgrades_use_installed_hash_and_uninstall_survives_package_change(monkeypatch):
    storage.install()
    skills.install()
    original = skills.source()
    monkeypatch.setattr(skills,'source',lambda: original+'\nUpdated guidance.\n')
    monkeypatch.setattr(skills,'SKILL_VERSION','0.2.0')
    with pytest.raises(RuntimeError):
        skills.verify()
    skills.install()
    assert skills.verify()
    monkeypatch.setattr(skills,'source',lambda: (_ for _ in ()).throw(RuntimeError('package unavailable')))
    skills.uninstall()
    assert not skills.target().exists()


def test_collision_duplicate_root_and_extra_files(tmp_path):
    storage.install()
    path = skills.target()
    path.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match='directory already exists'):
        skills.install()
    path.parent.rmdir()
    duplicate = tmp_path/'.agents/skills/base-layer-context'
    duplicate.mkdir(parents=True)
    (duplicate/'SKILL.md').write_text('unowned')
    with pytest.raises(RuntimeError, match='Another Context skill'):
        skills.install()
    (duplicate/'SKILL.md').unlink()
    duplicate.rmdir()
    skills.install()
    extra = path.parent/'user-note'
    extra.write_text('preserve')
    skills.uninstall()
    assert extra.read_text() == 'preserve'


def test_missing_file_repair_and_changed_codex_home(tmp_path, monkeypatch):
    storage.install()
    skills.install()
    path = skills.target()
    path.unlink()
    with pytest.raises(RuntimeError):
        skills.verify()
    skills.install()
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'other-codex'))
    with pytest.raises(RuntimeError):
        skills.verify()
    with pytest.raises(RuntimeError):
        skills.install()
    skills.uninstall()  # Always removes only the recorded installation root.
    assert not path.exists()


def test_unowned_symlink_is_preserved(tmp_path):
    storage.install()
    path = skills.target()
    foreign = tmp_path/'foreign'
    foreign.mkdir()
    path.parent.parent.mkdir(parents=True)
    path.parent.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(RuntimeError, match='symbolic link'):
        skills.install()
    assert list(foreign.iterdir()) == []


def test_disabled_skill_is_not_reported_ready():
    storage.install()
    skills.install()
    config = skills.root()/'config.toml'
    config.write_text('[[skills.config]]\npath = '+json.dumps(str(skills.target()))+'\nenabled = false\n')
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match='disabled'):
        skills.verify()
    with pytest.raises(RuntimeError, match='disabled'):
        skills.install()
    skills.uninstall()
    assert config.read_bytes() == before
