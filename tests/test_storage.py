import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bl_context import storage


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.read_bytes())
            for p in root.rglob('*') if p.is_file()}


def test_real_cli_lifecycle(tmp_path):
    cli = str(Path(sys.executable).parent / 'blctx')
    original = tmp_path / '.codex/sessions/original.jsonl'
    original.parent.mkdir(parents=True)
    original.write_text('original transcript')
    unrelated = tmp_path / '.codex/config.toml'
    unrelated.write_text('user configuration')
    records = []

    def run(args, code):
        before = snapshot(tmp_path)
        result = subprocess.run([cli, *args, '--json'], cwd='/', env=os.environ.copy(),
                                text=True, capture_output=True)
        assert result.returncode == code, result.stderr + result.stdout
        data = json.loads(result.stdout)
        assert data['exit_code'] == code and not data['ready']
        if args[0] in ('status', 'doctor'):
            assert snapshot(tmp_path) == before
        assert original.read_text() == 'original transcript'
        assert unrelated.read_text() == 'user configuration'
        records.append({'command': ['blctx', *args, '--json'], 'exit_code': code, 'json': data})

    for cmd in ('status', 'doctor'):
        run([cmd, '--step', 'data_directory'], 1)
    paths = storage.locations()
    for cycle in range(2):
        run(['install', 'codex', '--step', 'data_directory'], 0)
        manifest = storage.read_manifest(paths)
        before = snapshot(tmp_path)
        run(['install', 'codex', '--step', 'data_directory'], 0)
        assert snapshot(tmp_path) == before
        for cmd in ('status', 'doctor'):
            run([cmd, '--step', 'data_directory'], 0)
        run(['install', 'codex', '--step', 'embedding_model'], 1)
        run(['uninstall', 'codex'], 0)
        assert storage.read_manifest(paths)['installation_id'] == manifest['installation_id']
        assert storage.database_path(paths).exists()
        for cmd in ('status', 'doctor'):
            run([cmd, '--step', 'data_directory'], 1)
    unknown = paths['cache'] / 'user-file'
    unknown.write_text('preserve')
    run(['uninstall', 'codex', '--purge'], 0)
    assert unknown.read_text() == 'preserve'
    assert not storage.database_path(paths).exists()
    assert not storage.manifest_path(paths).exists()
    for cmd in ('status', 'doctor'):
        run([cmd, '--step', 'data_directory'], 1)
    run(['install', 'codex', '--step', 'data_directory'], 0)
    if os.environ.get('BLCTX_TEST_EVIDENCE'):
        Path(os.environ['BLCTX_TEST_EVIDENCE']).write_text(json.dumps({
            'environment': 'Isolated HOME, Linux, real installed blctx entry point; cwd=/',
            'preservation': 'Original transcripts, unrelated config and unknown cache file preserved. Status/doctor snapshots unchanged.',
            'records': records,
        }, indent=2) + '\n')


@pytest.mark.parametrize('kind', ['directory_permissions', 'file_permissions', 'schema', 'manifest', 'inactive', 'interpreter'])
def test_damage_fails_without_repair(tmp_path, kind):
    storage.install()
    paths = storage.locations()
    if kind == 'directory_permissions':
        paths['cache'].chmod(0o755)
    elif kind == 'file_permissions':
        storage.database_path(paths).chmod(0o644)
    elif kind == 'schema':
        with sqlite3.connect(storage.database_path(paths)) as db:
            db.execute('DROP TABLE context_metadata')
    else:
        manifest = storage.read_manifest(paths)
        if kind == 'manifest':
            manifest['schema_version'] = 99
        elif kind == 'inactive':
            manifest['state'] = 'inactive'
        else:
            manifest['interpreter'] = '/missing/python'
        storage.atomic_manifest(paths, manifest)
    before = snapshot(tmp_path)
    with pytest.raises(Exception):
        storage.verify()
    assert snapshot(tmp_path) == before


def test_unknown_database_is_never_claimed(tmp_path):
    paths = storage.locations()
    paths['data'].mkdir(parents=True, mode=0o700)
    db = storage.database_path(paths)
    db.write_text('unrelated database')
    with pytest.raises(RuntimeError, match='Unowned'):
        storage.install()
    assert db.read_text() == 'unrelated database'
    assert not storage.manifest_path(paths).exists()
    storage.uninstall(purge=True)
    assert db.exists()


@pytest.mark.parametrize('target', ['directory', 'database', 'manifest'])
def test_symlinks_refused(tmp_path, target):
    outside = tmp_path / 'outside'
    outside.write_text('do not touch')
    paths = storage.locations()
    if target == 'directory':
        paths['data'].parent.mkdir(parents=True)
        paths['data'].symlink_to(tmp_path, target_is_directory=True)
    else:
        storage.install()
        path = storage.database_path(paths) if target == 'database' else storage.manifest_path(paths)
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(RuntimeError, match='symbolic link'):
        storage.install()
    with pytest.raises(RuntimeError, match='symbolic link'):
        storage.uninstall(purge=True)
    assert outside.read_text() == 'do not touch'


def test_manifest_cannot_authorize_arbitrary_delete(tmp_path):
    storage.install()
    paths = storage.locations()
    outside = tmp_path / 'unrelated'
    outside.write_text('keep')
    manifest = storage.read_manifest(paths)
    manifest['owned_files'].append(str(outside))
    storage.atomic_manifest(paths, manifest)
    with pytest.raises(RuntimeError, match='manifest'):
        storage.uninstall(purge=True)
    assert outside.read_text() == 'keep'


def test_failed_schema_creation_recovers(tmp_path, monkeypatch):
    original = storage.initialize_database
    def fail(*args):
        raise OSError('simulated interruption')
    monkeypatch.setattr(storage, 'initialize_database', fail)
    with pytest.raises(OSError):
        storage.install()
    paths = storage.locations()
    assert storage.read_manifest(paths)['state'] == 'installing'
    assert not storage.database_path(paths).exists()
    with pytest.raises(RuntimeError, match='inactive'):
        storage.verify()
    monkeypatch.setattr(storage, 'initialize_database', original)
    storage.install()
    storage.verify()


def test_custom_xdg_and_existing_unknown_files(tmp_path, monkeypatch):
    for kind in ('data', 'config', 'cache', 'state'):
        monkeypatch.setenv(f'XDG_{kind.upper()}_HOME', str(tmp_path / ('custom-' + kind)))
    paths = storage.locations()
    paths['data'].mkdir(parents=True, mode=0o700)
    unknown = paths['data'] / 'keep'
    unknown.write_text('keep')
    storage.install()
    storage.verify()
    assert str(paths['data']) not in storage.read_manifest(paths)['owned_directories']
    storage.uninstall(purge=True)
    assert unknown.read_text() == 'keep'
    assert paths['data'].exists()


def test_relative_xdg_uses_home(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setenv('XDG_DATA_HOME', 'relative')
    assert storage.locations()['data'] == tmp_path / '.local/share/bl-context'


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_native_paths_and_child_environment(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(sys, 'platform', platform)
    paths = storage.locations()
    if platform == 'darwin':
        support = tmp_path / 'Library/Application Support/bl-context'
        assert paths == {
            'data': support / 'data', 'config': support / 'config',
            'state': support / 'state', 'cache': tmp_path / 'Library/Caches/bl-context',
        }
    else:
        assert paths['state'] == tmp_path / '.local/state/bl-context'
    environment = storage.environment(paths)
    monkeypatch.setenv('HOME', str(tmp_path / 'another host'))
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert storage.locations() == paths


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_xdg_overrides_native_defaults(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'custom data'))
    monkeypatch.setenv('XDG_STATE_HOME', 'relative')
    paths = storage.locations()
    assert paths['data'] == tmp_path / 'custom data/bl-context'
    assert paths['state'].is_relative_to(tmp_path)
    monkeypatch.setenv('BLCTX_STATE_DIR', str(tmp_path / 'pinned state'))
    assert storage.locations()['state'] == tmp_path / 'pinned state'
    monkeypatch.setenv('BLCTX_STATE_DIR', 'relative')
    with pytest.raises(RuntimeError, match='must be absolute'):
        storage.locations()


def test_unsupported_platform_is_actionable(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    with pytest.raises(RuntimeError, match='Linux and macOS'):
        storage.locations()


def test_socket_limit_counts_encoded_bytes(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    with pytest.raises(RuntimeError, match='shorter absolute XDG_STATE_HOME'):
        storage.socket_path({'state': Path('/' + 'é' * 47)})


def test_explicit_disconnect_supersedes_pending_index_jobs():
    storage.install()
    paths = storage.locations()
    with sqlite3.connect(storage.database_path(paths)) as db:
        db.execute(
            "CREATE TABLE ingest_jobs (id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        db.executemany(
            "INSERT INTO ingest_jobs VALUES (?, ?)",
            [("queued", "queued"), ("running", "running"), ("done", "complete")],
        )

    assert storage.cancel_pending_index_jobs(paths) == 2

    with sqlite3.connect(storage.database_path(paths)) as db:
        assert dict(db.execute("SELECT id, state FROM ingest_jobs")) == {
            "queued": "superseded",
            "running": "superseded",
            "done": "complete",
        }


def test_schema_publication_failure_is_atomic(tmp_path, monkeypatch):
    original = os.link
    def fail(*args):
        raise OSError('publication interrupted')
    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(OSError, match='publication'):
        storage.install()
    paths = storage.locations()
    assert list(paths['data'].iterdir()) == []
    assert storage.read_manifest(paths)['state'] == 'installing'
    monkeypatch.setattr(os, 'link', original)
    storage.install()
    storage.verify()


def test_missing_database_reinstall_recovers(tmp_path):
    storage.install()
    paths = storage.locations()
    installation_id = storage.read_manifest(paths)['installation_id']
    storage.database_path(paths).unlink()
    with pytest.raises(FileNotFoundError):
        storage.verify()
    assert not storage.database_path(paths).exists()
    storage.install()
    storage.verify()
    assert storage.read_manifest(paths)['installation_id'] == installation_id


def test_read_only_directory_is_not_green(tmp_path):
    storage.install()
    paths = storage.locations()
    paths['cache'].chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match='permissions'):
            storage.verify()
    finally:
        paths['cache'].chmod(0o700)


def test_purge_preserves_preexisting_empty_directory(tmp_path):
    paths = storage.locations()
    paths['config'].mkdir(parents=True, mode=0o700)
    storage.install()
    storage.uninstall(purge=True)
    assert paths['config'].is_dir()
    assert list(paths['config'].iterdir()) == []


def test_replaced_database_is_not_purged(tmp_path):
    storage.install()
    paths = storage.locations()
    with sqlite3.connect(storage.database_path(paths)) as db:
        db.execute("UPDATE context_metadata SET value='another owner'")
    before = storage.database_path(paths).read_bytes()
    with pytest.raises(RuntimeError, match='does not belong'):
        storage.uninstall(purge=True)
    assert storage.database_path(paths).read_bytes() == before


def test_aliasing_xdg_paths_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'base'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'other/../base'))
    with pytest.raises(RuntimeError, match='separate'):
        storage.install()
