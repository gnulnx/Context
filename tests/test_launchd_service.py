import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from bl_context import launchd_service as launchd
from bl_context import service, storage, systemd_service
from bl_context.service_runtime import wait_for


def installed_definition():
    storage.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    unit = launchd.unit_path(manifest)
    unit.parent.mkdir(parents=True, exist_ok=True)
    launchd.write_definition(unit, launchd.definition(paths, manifest))
    manifest['service_unit'] = str(unit)
    storage.atomic_manifest(paths, manifest)
    return paths, manifest, unit


def report_for(unit, pid=123):
    return f'gui/501/example = {{\n\tpath = {unit}\n\tstate = running\n\tpid = {pid}\n}}'


def test_backends_are_selected_independently_of_provider():
    assert service.backend_for('linux') is systemd_service
    assert service.backend_for('darwin') is launchd
    with pytest.raises(RuntimeError, match='Linux and macOS'):
        service.backend_for('win32')


def test_plist_uses_literal_paths_private_logs_and_login_startup(tmp_path):
    paths, manifest, unit = installed_definition()
    manifest['interpreter'] = str(tmp_path / 'Python & tools $/bin/python')
    definition = launchd.definition(paths, manifest)
    assert plistlib.loads(plistlib.dumps(definition)) == definition
    assert definition['ProgramArguments'] == [manifest['interpreter'], '-m', 'bl_context.daemon',
                                              '--installation-id', manifest['installation_id']]
    assert unit.parent == tmp_path / 'Library/LaunchAgents'
    assert definition['EnvironmentVariables'] == {**storage.environment(paths), 'HOME': str(tmp_path)}
    assert definition['KeepAlive'] == {'SuccessfulExit': False}
    assert definition['RunAtLoad'] is True
    assert definition['Umask'] == 0o077 and definition['ExitTimeOut'] == 5
    launchd.prepare_log(paths, manifest)
    storage.private(paths['state'] / 'daemon.log')
    storage.private(unit)


def test_verify_checks_loaded_identity_and_disabled_state(tmp_path, monkeypatch):
    paths, manifest, unit = installed_definition()
    calls = []

    def manager(*args, **kwargs):
        calls.append(args)
        return report_for(unit) if args[0] == 'print' else ''

    response = {'installation_id': manifest['installation_id'], 'pid': 123, 'protocol_version': 1,
                'interpreter': manifest['interpreter'], 'runtime_fingerprint': launchd.runtime_fingerprint()}
    monkeypatch.setattr(launchd, 'manager', manager)
    monkeypatch.setattr(launchd, 'identity', lambda _: response)
    assert 'PID 123' in launchd.verify()
    assert all(call[0].startswith('print') for call in calls)
    real_interpreter = tmp_path / 'real-python'
    real_interpreter.touch()
    real_interpreter.chmod(0o700)
    interpreter_alias = tmp_path / 'Python with spaces'
    interpreter_alias.symlink_to(real_interpreter)
    manifest['interpreter'] = str(interpreter_alias)
    storage.atomic_manifest(paths, manifest)
    launchd.write_definition(unit, launchd.definition(paths, manifest))
    response['interpreter'] = str(real_interpreter)
    assert 'PID 123' in launchd.verify()
    response['interpreter'] = str(tmp_path / 'different-python')
    with pytest.raises(RuntimeError, match='identity does not match'):
        launchd.verify()
    response['interpreter'] = str(real_interpreter)
    response['pid'] = 124
    with pytest.raises(RuntimeError, match='identity does not match'):
        launchd.verify()
    response['pid'] = 123
    response['runtime_fingerprint'] = 'old'
    with pytest.raises(RuntimeError, match='stale'):
        launchd.verify()
    monkeypatch.setattr(launchd, 'manager', lambda *args, **kwargs: f'"{launchd.label(manifest)}" => disabled')
    with pytest.raises(RuntimeError, match='disabled'):
        launchd.verify()


def test_install_reuses_live_runtime_and_restarts_stale_code(monkeypatch):
    paths, manifest, unit = installed_definition()
    calls = []
    loaded = True

    def manager(*args, **kwargs):
        nonlocal loaded
        calls.append(args)
        if args[0] in ('bootout', 'bootstrap'):
            loaded = args[0] == 'bootstrap'
        return (report_for(unit) if loaded else None) if args[0] == 'print' else ''

    response = {'installation_id': manifest['installation_id'], 'runtime_fingerprint': launchd.runtime_fingerprint()}
    monkeypatch.setattr(launchd, 'manager', manager)
    monkeypatch.setattr(launchd, 'identity', lambda _: response)
    monkeypatch.setattr(launchd, 'verify', lambda: 'ready')
    launchd.install()
    assert not any(call[0] in ('bootout', 'bootstrap') for call in calls)
    calls.clear()
    response['runtime_fingerprint'] = 'old'
    launchd.install()
    assert ('bootout', launchd.target(manifest)) in calls
    assert ('bootstrap', launchd.domain(), str(unit)) in calls


def test_install_reloads_after_interpreter_change(monkeypatch):
    paths, manifest, unit = installed_definition()
    previous = {**manifest, 'interpreter': '/old environment/bin/python'}
    launchd.write_definition(unit, launchd.definition(paths, previous))
    calls = []
    loaded = True

    def manager(*args, **kwargs):
        nonlocal loaded
        calls.append(args)
        if args[0] in ('bootout', 'bootstrap'):
            loaded = args[0] == 'bootstrap'
        return (report_for(unit) if loaded else None) if args[0] == 'print' else ''

    monkeypatch.setattr(launchd, 'manager', manager)
    monkeypatch.setattr(launchd, 'identity', lambda _: {
        'installation_id': manifest['installation_id'], 'runtime_fingerprint': launchd.runtime_fingerprint(),
    })
    monkeypatch.setattr(launchd, 'verify', lambda: 'ready')
    launchd.install()
    assert launchd.read_definition(unit) == launchd.definition(paths, manifest)
    assert ('bootout', launchd.target(manifest)) in calls
    assert ('bootstrap', launchd.domain(), str(unit)) in calls


@pytest.mark.parametrize('operation', ['install', 'uninstall', 'verify'])
def test_modified_plist_is_preserved(monkeypatch, operation):
    paths, manifest, unit = installed_definition()
    modified = {**launchd.definition(paths, manifest), 'KeepAlive': True}
    launchd.write_definition(unit, modified)
    calls = []
    monkeypatch.setattr(launchd, 'manager', lambda *args, **kwargs: calls.append(args) or '')
    with pytest.raises(RuntimeError, match='differs'):
        getattr(launchd, operation)()
    assert launchd.read_definition(unit) == modified
    assert all(call[0].startswith('print') for call in calls)


def test_foreign_registration_is_not_stopped(monkeypatch):
    paths, manifest, unit = installed_definition()
    calls = []
    monkeypatch.setattr(launchd, 'manager', lambda *args, **kwargs: calls.append(args) or report_for(Path('/foreign.plist')))
    with pytest.raises(RuntimeError, match='another path'):
        launchd.uninstall()
    assert unit.exists()
    assert all(call[0] == 'print' for call in calls)


def test_unowned_log_and_symlinked_plist_are_preserved(tmp_path, monkeypatch):
    paths, manifest, unit = installed_definition()
    log = paths['state'] / 'daemon.log'
    log.write_text('unrelated')
    log.chmod(0o600)
    with pytest.raises(RuntimeError, match='Unowned daemon.log'):
        launchd.prepare_log(paths, manifest)
    assert log.read_text() == 'unrelated'
    outside = tmp_path / 'original.plist'
    unit.rename(outside)
    unit.symlink_to(outside)
    monkeypatch.setattr(launchd, 'manager', lambda *args, **kwargs: '')
    with pytest.raises(RuntimeError, match='symbolic link'):
        launchd.install()
    with pytest.raises(RuntimeError, match='symbolic link'):
        launchd.uninstall()
    assert outside.exists()


def test_owned_log_is_retained_on_uninstall_and_removed_on_purge():
    storage.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    launchd.prepare_log(paths, manifest)
    storage.atomic_manifest(paths, manifest)
    log = paths['state'] / 'daemon.log'
    storage.uninstall()
    assert log.exists()
    storage.uninstall(purge=True)
    assert not log.exists()


def test_uninstall_handles_missing_plist_and_preserves_data(monkeypatch):
    paths, manifest, unit = installed_definition()
    unit.unlink()
    calls = []
    monkeypatch.setattr(launchd, 'manager', lambda *args, **kwargs: calls.append(args))
    launchd.uninstall()
    assert 'service_unit' not in storage.read_manifest(paths)
    assert storage.database_path(paths).exists()
    launchd.uninstall()
    assert all(call[0] == 'print' for call in calls)


def test_missing_manager_domain_is_not_treated_as_absent_service(monkeypatch):
    # Exercise the actual wrapper despite the unit-test isolation fixture.
    manager = REAL_MANAGER
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 113, '', 'Could not find domain for user gui: 501',
    ))
    with pytest.raises(RuntimeError, match='logged-in macOS desktop'):
        manager('print', 'gui/501/example', missing_ok=True)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 113, '', 'Could not find service "example" in domain for user gui: 501',
    ))
    assert manager('print', 'gui/501/example', missing_ok=True) is None


REAL_MANAGER = launchd.manager


@pytest.mark.skipif(sys.platform != 'darwin' or os.environ.get('BLCTX_LAUNCHD_TEST') != '1',
                    reason='Opt-in macOS desktop LaunchAgent acceptance')
def test_real_launchd_lifecycle(tmp_path, monkeypatch):
    # Includes spaces in HOME and the executable, like native Library paths and
    # user-managed Python environments. LaunchAgent scope remains this UUID only.
    home = tmp_path / 'user home'
    home.mkdir()
    environment = tmp_path / 'python with spaces'
    environment.symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    interpreter = environment / 'bin' / Path(sys.executable).name
    monkeypatch.setenv('HOME', str(home))
    storage.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    manifest['interpreter'] = str(interpreter)
    storage.atomic_manifest(paths, manifest)
    unit = launchd.unit_path(manifest)
    sentinel = paths['data'] / 'unrelated.txt'
    sentinel.write_text('keep')
    try:
        with pytest.raises(RuntimeError, match='ownership'):
            service.verify()
        for cycle in range(2):
            service.install()
            first = service.identity(paths)['pid']
            service.install()
            assert service.identity(paths)['pid'] == first
            assert 'verified' in service.health()
            os.kill(first, signal.SIGKILL)
            wait_for(service.verify)
            replacement = service.identity(paths)['pid']
            assert replacement != first
            if not cycle:
                # A real Python-environment upgrade must replace the loaded job,
                # since kickstart alone would retain its old ProgramArguments.
                manifest = storage.read_manifest(paths)
                manifest['interpreter'] = sys.executable
                storage.atomic_manifest(paths, manifest)
                service.install()
                replacement_after_upgrade = service.identity(paths)['pid']
                assert replacement_after_upgrade != replacement
                replacement = replacement_after_upgrade
            if cycle:
                unit.unlink()
            service.uninstall()
            assert not unit.exists()
            assert launchd.registered(manifest) is None
            assert not (paths['state'] / 'daemon.sock').exists()
            with pytest.raises(ProcessLookupError):
                os.kill(replacement, 0)
            assert storage.database_path(paths).exists()
            assert sentinel.read_text() == 'keep'
        service.uninstall()
    except Exception:
        log = paths['state'] / 'daemon.log'
        if log.exists():
            print(log.read_text())
        raise
    finally:
        service.uninstall()
        storage.uninstall(purge=True)
