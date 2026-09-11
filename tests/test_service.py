import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bl_context import retrieval_cli, service, storage

REAL_BUS = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
REAL_RUNTIME = os.environ.get('XDG_RUNTIME_DIR')


def wait_for(probe, timeout=12):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return probe()
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def test_daemon_identity_single_writer_and_shutdown(tmp_path):
    storage.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    command = [sys.executable, '-m', 'bl_context.daemon', '--installation-id', manifest['installation_id']]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        info = wait_for(lambda: service.identity(paths))
        assert info['pid'] == child.pid
        health = retrieval_cli.call({'operation': 'health'})
        assert health == {
            'status': 'ok',
            'installation_id': manifest['installation_id'],
            'schema_version': storage.VERSION,
            'running_jobs': 0,
            'authored_updates_pending': 0,
            'index_state': 'ready',
            'query_mode': 'hybrid',
        }
        second = subprocess.run(command, capture_output=True, timeout=5)
        assert second.returncode != 0
        assert service.identity(paths) == info
    finally:
        child.terminate()
        child.communicate(timeout=5)
    assert child.returncode == 0
    assert not (paths['state'] / 'daemon.sock').exists()


def test_unavailable_manager_is_actionable():
    storage.install()
    with pytest.raises(RuntimeError, match='systemd user session'):
        service.install()
    assert not list(storage.locations()['config'].iterdir())


@pytest.mark.skipif(os.environ.get('BLCTX_SYSTEMD_TEST') != '1', reason='Requires explicit real user manager acceptance run')
def test_real_systemd_lifecycle(tmp_path, monkeypatch):
    for key, value in [('DBUS_SESSION_BUS_ADDRESS', REAL_BUS), ('XDG_RUNTIME_DIR', REAL_RUNTIME)]:
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    cli = str(Path(sys.executable).parent / 'blctx')
    records = []
    sentinel = tmp_path / 'original-transcript'
    sentinel.write_text('preserve')
    def run(args, expected):
        result = subprocess.run([cli, *args, '--json'], capture_output=True, text=True, timeout=45)
        assert result.returncode == expected, result.stdout + result.stderr
        data = json.loads(result.stdout)
        assert data['ready'] is False
        records.append({'command': ['blctx', *args, '--json'], 'exit_code': result.returncode, 'json': data})
        assert sentinel.read_text() == 'preserve'
    try:
        run(['status', '--step', 'background_service'], 1)
        for _ in range(2):
            run(['install', 'codex', '--step', 'background_service'], 0)
            paths = storage.locations()
            manifest = storage.read_manifest(paths)
            name = service.unit_name(manifest)
            run(['status', '--step', 'background_service'], 0)
            run(['doctor', '--step', 'background_service'], 0)
            pid = service.identity(paths)['pid']
            run(['install', 'codex', '--step', 'background_service'], 0)
            assert service.identity(paths)['pid'] == pid
            os.kill(pid, signal.SIGKILL)
            wait_for(service.verify)
            replacement = service.identity(paths)['pid']
            assert replacement != pid
            records.append({'restart': {'old_pid': pid, 'new_pid': replacement}})
            logs = subprocess.run(['journalctl', '--user', '-u', name, '--no-pager'], capture_output=True, text=True, timeout=5)
            assert manifest['installation_id'] in logs.stdout
            if _ == 1:
                (paths['config'] / name).unlink()
            run(['uninstall', 'codex'], 0)
            assert not Path(f'/proc/{replacement}').exists()
            assert not (paths['state'] / 'daemon.sock').exists()
            assert not (paths['config'] / name).exists()
            assert service.manager('show', name, '--property=LoadState', '--value') == 'not-found'
            run(['status', '--step', 'background_service'], 1)
            run(['doctor', '--step', 'background_service'], 1)
        if os.environ.get('BLCTX_TEST_EVIDENCE'):
            Path(os.environ['BLCTX_TEST_EVIDENCE']).write_text(json.dumps(records, indent=2) + '\n')
    finally:
        service.uninstall()
        storage.uninstall(purge=True)


def test_service_unit_uses_absolute_paths_and_bounded_lifecycle():
    storage.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    text = service.unit_text(paths, manifest)
    assert manifest['interpreter'] in text
    assert 'Type=notify' in text and 'TimeoutStartSec=10' in text
    assert 'TimeoutStopSec=5' in text and 'Restart=on-failure' in text
    assert 'UMask=0077' in text and 'WorkingDirectory=/' in text
    assert service.quote('a$b%') == '"a$b%%"'
    assert service.quote('a$b%', command=True) == '"a$$b%%"'


def test_daemon_refuses_foreign_endpoint(tmp_path):
    storage.install()
    paths = storage.locations()
    endpoint = paths['state'] / 'daemon.sock'
    endpoint.write_text('unrelated')
    manifest = storage.read_manifest(paths)
    result = subprocess.run([sys.executable, '-m', 'bl_context.daemon', '--installation-id', manifest['installation_id']],
                            capture_output=True, timeout=5)
    assert result.returncode != 0
    assert endpoint.read_text() == 'unrelated'


def test_install_restarts_stale_runtime_only(monkeypatch):
    storage.install()
    paths = storage.locations()
    calls = []
    monkeypatch.setattr(service, 'manager', lambda *args: calls.append(args) or '')
    monkeypatch.setattr(service, 'verify', lambda: None)
    identifier = storage.read_manifest(paths)['installation_id']
    monkeypatch.setattr(service, 'identity', lambda _: {'installation_id': identifier})
    service.install()
    assert ('restart', service.unit_name(storage.read_manifest(paths))) in calls
    calls.clear()
    monkeypatch.setattr(service, 'identity', lambda _: {'installation_id': identifier, 'runtime_fingerprint': service.runtime_fingerprint()})
    service.install()
    assert not any(c[0] == 'restart' for c in calls)


def test_install_migrates_generated_interpreter_but_preserves_custom_unit(monkeypatch):
    storage.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    unit = paths['config'] / service.unit_name(manifest)
    previous = {**manifest, 'interpreter': '/old/environment/bin/python'}
    unit.write_text(service.unit_text(paths, previous))
    unit.chmod(0o600)
    calls = []
    monkeypatch.setattr(service, 'manager', lambda *args: calls.append(args) or '')
    monkeypatch.setattr(service, 'identity', lambda _: {'installation_id': manifest['installation_id'], 'runtime_fingerprint': service.runtime_fingerprint()})
    monkeypatch.setattr(service, 'verify', lambda: None)
    service.install()
    assert unit.read_text() == service.unit_text(paths, manifest)
    assert ('restart', unit.name) in calls
    modified = unit.read_text().replace('RestartSec=1', 'RestartSec=9')
    unit.write_text(modified)
    with pytest.raises(RuntimeError, match='differs'):
        service.install()
    assert unit.read_text() == modified
