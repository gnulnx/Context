"""Linux systemd user service adapter; all operations are installation-scoped."""
import fcntl
import json
import os
import socket
import stat
import subprocess
from pathlib import Path

from . import storage
from .runtime_version import runtime_fingerprint


def manager(*args):
    try:
        result = subprocess.run(['systemctl', '--user', *args], text=True,
                                capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('User service manager unavailable; use a Linux login with systemd --user and a session bus.') from exc
    if result.returncode:
        raise RuntimeError(f'systemctl {" ".join(args)} failed: {result.stderr.strip() or result.stdout.strip()}. '
                           'Check your systemd user session and journalctl --user.')
    return result.stdout.strip()


def unit_name(manifest):
    return f'blctxd-{manifest["installation_id"]}.service'


def quote(value, *, command=False):
    if '\n' in value or '\r' in value or '\0' in value:
        raise RuntimeError('Service paths must not contain control characters')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (value.replace('$', '$$') if command else value) + '"'


def unit_text(paths, manifest):
    environment = {}
    environment.update({f'XDG_{key.upper()}_HOME': str(path.parent) for key, path in paths.items()})
    env_lines = '\n'.join('Environment=' + quote(f'{key}={value}') for key, value in environment.items())
    return f'''# bl-context installation {manifest['installation_id']}
[Unit]
Description=Base Layer Context daemon
[Service]
Type=notify
ExecStart={quote(manifest['interpreter'], command=True)} -m bl_context.daemon --installation-id {manifest['installation_id']}
{env_lines}
WorkingDirectory=/
UMask=0077
Restart=on-failure
RestartSec=1
TimeoutStartSec=10
TimeoutStopSec=5
KillMode=control-group
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=default.target
'''


def identity(paths):
    endpoint = paths['state'] / 'daemon.sock'
    info = endpoint.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('IPC endpoint is not a private user-owned socket')
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(2)
        channel.connect(str(endpoint))
        channel.sendall(b'identity\n')
        return json.loads(channel.recv(4096))


def verify():
    storage.verify()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    unit = paths['config'] / unit_name(manifest)
    if manifest.get('service_unit') != str(unit):
        raise RuntimeError('Service installation ownership is absent')
    storage.private(unit)
    if unit.read_text() != unit_text(paths, manifest):
        raise RuntimeError('Service unit does not match this installation')
    if manager('is-enabled', unit.name) != 'enabled':
        raise RuntimeError('User service is not persistently enabled')
    properties = dict(line.split('=', 1) for line in manager('show', unit.name,
        '--property=ActiveState,SubState,MainPID,FragmentPath').splitlines())
    if (properties.get('ActiveState') != 'active' or properties.get('SubState') != 'running'
            or Path(properties.get('FragmentPath', '')).resolve() != unit.resolve()):
        raise RuntimeError('Expected registered service is not running')
    response = identity(paths)
    pid = int(properties['MainPID'])
    if any(response.get(k) != v for k, v in {'installation_id': manifest['installation_id'], 'pid': pid, 'protocol_version': 1}.items()):
        raise RuntimeError('Running daemon identity does not match systemd')
    if response.get('runtime_fingerprint') != runtime_fingerprint():
        raise RuntimeError('Daemon code is stale; run blctx install codex --step background_service to restart it')
    if Path(f'/proc/{pid}/exe').resolve() != Path(manifest['interpreter']).resolve():
        raise RuntimeError('Running interpreter does not match installation')
    command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    expected = [manifest['interpreter'], '-m', 'bl_context.daemon', '--installation-id', manifest['installation_id']]
    if command[:-1] != [part.encode() for part in expected]:
        raise RuntimeError('Running executable does not match installation')
    return f'Enabled user service and daemon identity verified (PID {pid}).'


def install():
    storage.verify()
    paths = storage.locations()
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        unit = paths['config'] / unit_name(manifest)
        content = unit_text(paths, manifest)
        manager('show-environment')
        storage.safe_path(unit)
        interpreter_changed = False
        if unit.exists():
            storage.private(unit)
            if unit.read_text() != content:
                old_lines = unit.read_text().splitlines()
                new_lines = content.splitlines()
                old_exec = [line for line in old_lines if line.startswith('ExecStart=')]
                suffix = f' -m bl_context.daemon --installation-id {manifest["installation_id"]}'
                if (len(old_exec) != 1 or not old_exec[0].startswith('ExecStart="')
                        or not old_exec[0].endswith('"' + suffix)
                        or [line for line in old_lines if not line.startswith('ExecStart=')] != [line for line in new_lines if not line.startswith('ExecStart=')]):
                    raise RuntimeError('Existing service unit differs; refusing to overwrite it')
                unit.write_text(content)
                interpreter_changed = True
        else:
            with unit.open('x') as stream:
                os.chmod(unit, 0o600)
                stream.write(content)
        manifest['service_unit'] = str(unit)
        storage.atomic_manifest(paths, manifest)
        manager('enable', str(unit))
        manager('daemon-reload')
        manager('start', unit.name)
    # Stop/restart outside the manifest lock: a draining worker may need it.
    response = identity(paths)
    if response.get('installation_id') != manifest['installation_id']:
        raise RuntimeError('Unexpected daemon ownership; refusing automatic restart')
    if interpreter_changed or response.get('runtime_fingerprint') != runtime_fingerprint():
        manager('restart', unit.name)
    verify()


def uninstall():
    paths = storage.locations()
    if not storage.manifest_path(paths).exists():
        return
    manifest = storage.read_manifest(paths)
    unit = paths['config'] / unit_name(manifest)
    if 'service_unit' not in manifest:
        return
    if manifest['service_unit'] != str(unit):
        raise RuntimeError('Invalid service ownership')
    storage.safe_path(unit)
    if unit.exists():
        storage.private(unit)
        if unit.read_text() != unit_text(paths, manifest):
            raise RuntimeError('Refusing to remove a modified service unit')
    if manager('show', unit.name, '--property=LoadState', '--value') != 'not-found':
        manager('stop', unit.name)
    manager('disable', unit.name)
    endpoint = paths['state'] / 'daemon.sock'
    if endpoint.exists():
        # A killed daemon can leave its socket; acquire the writer lock first.
        fd = os.open(paths['data'], os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = endpoint.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('Refusing unknown IPC artifact')
            endpoint.unlink()
        finally:
            os.close(fd)
    unit.unlink(missing_ok=True)
    manager('daemon-reload')
    if manager('show', unit.name, '--property=LoadState', '--value') != 'not-found':
        raise RuntimeError('Service registration remains after uninstall')
    manifest.pop('service_unit')
    storage.atomic_manifest(paths, manifest)
