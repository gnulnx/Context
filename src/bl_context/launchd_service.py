"""Per-user macOS LaunchAgent backend, independent of any model provider."""
import fcntl
import os
import plistlib
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from . import storage
from .runtime_version import runtime_fingerprint
from .service_runtime import identity, wait_for

DIAGNOSTICS = (
    'Inspect daemon.log in the Context state directory. On macOS, check '
    'System Settings > General > Login Items & Extensions for background permissions.'
)


def manager(*args, missing_ok=False):
    try:
        result = subprocess.run(
            ['/bin/launchctl', *args], text=True, capture_output=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('launchd unavailable; install from a logged-in macOS desktop session.') from exc
    if result.returncode:
        # launchctl print returns 113 for an absent service. Never treat
        # permission failures or an unavailable login domain as absence.
        if missing_ok and result.returncode == 113 and 'Could not find service' in result.stderr:
            return None
        raise RuntimeError(
            f'launchctl {" ".join(args)} failed: {result.stderr.strip() or result.stdout.strip()}. '
            'Use a logged-in macOS desktop session. ' + DIAGNOSTICS
        )
    return result.stdout.strip()


def domain():
    return f'gui/{os.getuid()}'


def label(manifest):
    return f'com.baselayer.context.{manifest["installation_id"]}'


def target(manifest):
    return f'{domain()}/{label(manifest)}'


def unit_path(manifest):
    # launchd loads this directory at login, regardless of XDG_CONFIG_HOME.
    return Path.home() / 'Library/LaunchAgents' / f'{label(manifest)}.plist'


def definition(paths, manifest):
    return {
        'Label': label(manifest),
        'ProgramArguments': [manifest['interpreter'], '-m', 'bl_context.daemon',
                             '--installation-id', manifest['installation_id']],
        'EnvironmentVariables': {**storage.environment(paths), 'HOME': str(Path.home())},
        'WorkingDirectory': '/',
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 1,
        'ExitTimeOut': 5,
        'Umask': 0o077,
        'StandardOutPath': str(paths['state'] / 'daemon.log'),
        'StandardErrorPath': str(paths['state'] / 'daemon.log'),
    }


def read_definition(unit):
    storage.private(unit)
    try:
        return plistlib.loads(unit.read_bytes())
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise RuntimeError('Invalid LaunchAgent plist; preserving it.') from exc


def validate_definition(unit, expected, *, allow_interpreter_change=False):
    actual = read_definition(unit)
    if allow_interpreter_change and isinstance(actual, dict):
        args = actual.get('ProgramArguments')
        if (isinstance(args, list) and len(args) == 5
                and isinstance(args[0], str) and Path(args[0]).is_absolute()):
            actual = {**actual, 'ProgramArguments': [expected['ProgramArguments'][0], *args[1:]]}
    if actual != expected:
        raise RuntimeError('Existing LaunchAgent differs; refusing to overwrite or remove it.')


def registered(manifest):
    return manager('print', target(manifest), missing_ok=True)


def property_value(report, name):
    # Only top-level properties: ignore similarly named nested environment keys.
    match = re.search(r'^\t' + re.escape(name) + r' = (.+)$', report, re.MULTILINE)
    if not match:
        raise RuntimeError(f'launchd did not report {name}; cannot verify service identity.')
    return match.group(1)


def validate_registration(report, unit):
    if Path(property_value(report, 'path')) != unit:
        raise RuntimeError('Registered LaunchAgent belongs to another path; preserving it.')


def stop(manifest):
    manager('bootout', target(manifest))

    def unloaded():
        if registered(manifest) is not None:
            raise RuntimeError('LaunchAgent is still unloading')
    wait_for(unloaded)


def verify():
    storage.verify()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    unit = unit_path(manifest)
    if manifest.get('service_unit') != str(unit):
        raise RuntimeError('Service installation ownership is absent')
    validate_definition(unit, definition(paths, manifest))
    disabled = manager('print-disabled', domain())
    if re.search(r'"' + re.escape(label(manifest)) + r'"\s*=>\s*(true|disabled)', disabled):
        raise RuntimeError('LaunchAgent is disabled. ' + DIAGNOSTICS)
    report = registered(manifest)
    if report is None:
        raise RuntimeError('LaunchAgent is not loaded')
    validate_registration(report, unit)
    if property_value(report, 'state') != 'running':
        raise RuntimeError('LaunchAgent is not running. ' + DIAGNOSTICS)
    pid = int(property_value(report, 'pid'))
    response = identity(paths)
    expected = {'installation_id': manifest['installation_id'], 'pid': pid, 'protocol_version': 1,
                'interpreter': manifest['interpreter']}
    if any(response.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Running daemon identity does not match launchd')
    if response.get('runtime_fingerprint') != runtime_fingerprint():
        raise RuntimeError('Daemon code is stale; reinstall the background service to restart it')
    return f'Enabled LaunchAgent and daemon identity verified (PID {pid}).'


def prepare_log(paths, manifest):
    log = paths['state'] / 'daemon.log'
    storage.safe_path(log)
    if log.exists():
        if manifest.get('service_log') != str(log):
            raise RuntimeError('Unowned daemon.log already exists; preserving it.')
    else:
        # Persist ownership before creation so an interrupted install can resume.
        manifest['service_log'] = str(log)
        storage.atomic_manifest(paths, manifest)
        with log.open('x'):
            log.chmod(0o600)
    storage.private(log)
    manifest['service_log'] = str(log)


def write_definition(unit, content):
    fd, temporary = tempfile.mkstemp(prefix='.blctx-', dir=unit.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            plistlib.dump(content, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, unit)
        storage.sync_directory(unit.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install():
    storage.verify()
    paths = storage.locations()
    manager('print', domain())
    storage.socket_path(paths)
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        unit = unit_path(manifest)
        if manifest.get('service_unit') not in (None, str(unit)):
            raise RuntimeError('Service belongs to another location; uninstall it first.')
        storage.safe_path(unit)
        content = definition(paths, manifest)
        changed = False
        if unit.exists():
            validate_definition(unit, content, allow_interpreter_change=True)
            changed = read_definition(unit) != content
        report = registered(manifest)
        if report is not None:
            if manifest.get('service_unit') != str(unit):
                raise RuntimeError('Unowned LaunchAgent registration; preserving it.')
            validate_registration(report, unit)
        unit.parent.mkdir(parents=True, exist_ok=True)
        prepare_log(paths, manifest)
        if not unit.exists() or changed:
            write_definition(unit, content)
        manifest['service_unit'] = str(unit)
        storage.atomic_manifest(paths, manifest)
    # Never drain a daemon while holding the manifest lock needed by its workers.
    if report is not None and changed:
        stop(manifest)
        report = None
    manager('enable', target(manifest))
    if report is None:
        manager('bootstrap', domain(), str(unit))
    else:
        manager('kickstart', target(manifest))
    response = wait_for(lambda: identity(paths))
    if response.get('installation_id') != manifest['installation_id']:
        raise RuntimeError('Unexpected daemon ownership; refusing automatic restart')
    if response.get('runtime_fingerprint') != runtime_fingerprint():
        stop(manifest)
        manager('bootstrap', domain(), str(unit))
    wait_for(verify)


def uninstall():
    paths = storage.locations()
    if not storage.manifest_path(paths).exists():
        return
    manifest = storage.read_manifest(paths)
    if 'service_unit' not in manifest:
        storage.cancel_pending_index_jobs(paths)
        return
    unit = unit_path(manifest)
    if manifest['service_unit'] != str(unit):
        raise RuntimeError('Invalid service ownership')
    storage.safe_path(unit)
    if unit.exists():
        validate_definition(unit, definition(paths, manifest))
    report = registered(manifest)
    if report is not None:
        validate_registration(report, unit)
        stop(manifest)

    def cleanup():
        # bootout may return before the process has released its writer lock.
        fd = os.open(paths['data'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            endpoint = paths['state'] / 'daemon.sock'
            if endpoint.exists() or endpoint.is_symlink():
                info = endpoint.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise RuntimeError('Refusing unknown IPC artifact')
                endpoint.unlink()
        finally:
            os.close(fd)
    wait_for(cleanup)
    if registered(manifest) is not None:
        raise RuntimeError('LaunchAgent registration remains after uninstall')
    unit.unlink(missing_ok=True)
    storage.cancel_pending_index_jobs(paths)
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        manifest.pop('service_unit')
        storage.atomic_manifest(paths, manifest)
