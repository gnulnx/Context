"""Shared private IPC and readiness checks for user-service backends."""
import json
import os
import socket
import stat
import time

from . import storage


def identity(paths):
    endpoint = storage.socket_path(paths)
    info = endpoint.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('IPC endpoint is not a private user-owned socket')
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(2)
        channel.connect(str(endpoint))
        channel.sendall(b'identity\n')
        return json.loads(channel.recv(4096))



def wait_for(probe, timeout=12):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return probe()
        except (OSError, ValueError, RuntimeError) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Daemon did not become ready: {exc}') from exc
            time.sleep(.1)
