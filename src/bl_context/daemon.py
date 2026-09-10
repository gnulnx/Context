"""Single-writer daemon with private Unix IPC and systemd readiness notification."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import stat

from . import storage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installation-id', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    storage.verify()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    if manifest['installation_id'] != args.installation_id:
        raise RuntimeError('Installation identity mismatch')
    # Directory flock avoids stale lock files and is shared by all daemon starts.
    writer = os.open(paths['data'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    endpoint = paths['state'] / 'daemon.sock'
    if endpoint.exists() or endpoint.is_symlink():
        info = endpoint.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError('Refusing unowned IPC endpoint')
        endpoint.unlink()
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(endpoint))
        os.chmod(endpoint, 0o600)
        server.listen(8)
        server.settimeout(0.25)
        notify = os.environ.get('NOTIFY_SOCKET')
        if notify:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.connect('\0' + notify[1:] if notify.startswith('@') else notify)
                channel.sendall(b'READY=1')
        logging.info('Ready installation=%s pid=%s', args.installation_id, os.getpid())
        while not stopping:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(1)
                try:
                    request = connection.recv(1024)
                    response = {'error': 'Unknown request'}
                    if request == b'identity\n':
                        response = {'installation_id': args.installation_id, 'pid': os.getpid(), 'protocol_version': 1}
                    connection.sendall(json.dumps(response).encode() + b'\n')
                except (OSError, ValueError):
                    logging.warning('IPC request failed')
    finally:
        server.close()
        endpoint.unlink(missing_ok=True)
        os.close(writer)
        logging.info('Stopped installation=%s', args.installation_id)


if __name__ == '__main__':
    main()
