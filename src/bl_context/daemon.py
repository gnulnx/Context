"""Single-writer daemon with private Unix IPC and systemd readiness notification."""
import argparse
import fcntl
import json
import logging
import os
import signal
import socket
import sqlite3
import stat
import threading
import time
from contextlib import closing

from . import storage
from .capture import reconcile
from .index import Index
from .runtime_version import runtime_fingerprint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installation-id', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    fingerprint = runtime_fingerprint()
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
    engine = None
    engine_lock = threading.Lock()
    slots = threading.BoundedSemaphore(8)

    def handle(connection):
        nonlocal engine
        with slots, connection:
            connection.settimeout(35)
            try:
                buffer = bytearray()
                while not buffer.endswith(b'\n'):
                    block = connection.recv(min(65536, 1048577 - len(buffer)))
                    if not block:
                        raise ValueError('Incomplete request')
                    buffer.extend(block)
                    if len(buffer) > 1048576:
                        raise ValueError('Request exceeds 1 MiB')
                if buffer == b'identity\n':
                    response = {'installation_id': args.installation_id, 'pid': os.getpid(), 'protocol_version': 1, 'runtime_fingerprint': fingerprint}
                else:
                    request = json.loads(buffer)
                    if not isinstance(request, dict):
                        raise ValueError('Expected JSON object')
                    with engine_lock:
                        if engine is None:
                            engine = Index(paths)
                    response = engine.submit(request)
            except Exception as exc:
                response = {'error': str(exc) or type(exc).__name__}
            try:
                connection.sendall(json.dumps(response).encode() + b'\n')
            except OSError:
                logging.warning('IPC client disconnected')

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
        with closing(sqlite3.connect(storage.database_path(paths))) as db:
            initialized = db.execute("SELECT 1 FROM sqlite_master WHERE name='ingest_jobs'").fetchone()
        if initialized or manifest.get('codex_hooks'):
            engine = Index(paths)
        logging.info('Ready installation=%s pid=%s', args.installation_id, os.getpid())
        capture_future = None
        next_capture = 0
        while not stopping:
            if time.monotonic() >= next_capture and (capture_future is None or capture_future.done()):
                next_capture = time.monotonic() + 2
                with engine_lock:
                    if engine is None and storage.read_manifest(paths).get('codex_hooks'):
                        engine = Index(paths)
                if engine is not None:
                    capture_future = engine.executor.submit(reconcile, engine)
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    finally:
        server.close()
        if engine is not None:
            engine.close()
        endpoint.unlink(missing_ok=True)
        os.close(writer)
        logging.info('Stopped installation=%s', args.installation_id)


if __name__ == '__main__':
    main()
