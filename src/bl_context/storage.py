"""Private Linux storage and the ownership boundary for installation lifecycle."""

import fcntl
import json
import os
import sqlite3
import stat
import sys
import sysconfig
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

VERSION = 1


def locations():
    if sys.platform != 'linux':
        raise RuntimeError('Local installation is currently supported on Linux only.')
    home = Path.home()
    if not home.is_absolute():
        raise RuntimeError('HOME must be an absolute path.')
    defaults = {'data': home / '.local/share', 'config': home / '.config',
                'cache': home / '.cache', 'state': home / '.local/state'}
    result = {}
    for kind, default in defaults.items():
        base = os.environ.get(f'XDG_{kind.upper()}_HOME', '')
        # XDG relative values are invalid; use the standard fallback.
        base = Path(base) if base and Path(base).is_absolute() else default
        safe_path(base)
        result[kind] = Path(os.path.abspath(base)) / 'bl-context'
    paths = list(result.values())
    if any(a == b or a in b.parents or b in a.parents
           for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise RuntimeError('Context data/config/cache/state paths must be separate.')
    return result


def safe_path(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise RuntimeError(f'Refusing symbolic link: {component}')


def private(path, directory=False):
    safe_path(path)
    info = path.stat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f'Not a user-owned {"directory" if directory else "file"}: {path}')
    mode = stat.S_IMODE(info.st_mode)
    required = 0o700 if directory else 0o600
    if mode & 0o077 or mode & required != required or not os.access(path, os.R_OK | os.W_OK):
        raise RuntimeError(f'Expected private writable permissions on {path}')
    if not directory and info.st_nlink != 1:
        raise RuntimeError(f'Refusing multiply linked file: {path}')


def manifest_path(paths):
    return paths['state'] / 'installation.json'


def database_path(paths):
    return paths['data'] / 'context.db'


def read_manifest(paths):
    path = manifest_path(paths)
    private(path)
    manifest = json.loads(path.read_text())
    expected_paths = {k: str(v) for k, v in paths.items()}
    if (manifest.get('schema_version') != VERSION
            or manifest.get('product') != 'bl-context'
            or manifest.get('state') not in ('installing', 'active', 'inactive')
            or manifest.get('directories') != expected_paths
            or manifest.get('owned_files') != [str(database_path(paths)), str(path)]):
        raise RuntimeError('Invalid or unsupported installation manifest; refusing to claim existing files.')
    uuid.UUID(manifest['installation_id'])
    owned_dirs = manifest.get('owned_directories')
    if (not isinstance(owned_dirs, list) or len(owned_dirs) != len(set(owned_dirs))
            or not set(owned_dirs) <= set(expected_paths.values())):
        raise RuntimeError('Invalid directory ownership in installation manifest.')
    for key in ('interpreter', 'executable'):
        value = manifest.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise RuntimeError(f'Invalid {key} in installation manifest.')
    return manifest


def atomic_manifest(paths, manifest):
    path = manifest_path(paths)
    fd, temporary = tempfile.mkstemp(prefix='.installation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(manifest, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(paths, timeout=None):
    # Lock an existing directory: no lock file, and verification never creates it.
    private(paths['state'], directory=True)
    fd = os.open(paths['state'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Context ownership lock is busy') from None
                    time.sleep(.01)
        yield
    finally:
        os.close(fd)


def validate_database(paths, installation_id):
    path = database_path(paths)
    private(path)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        columns = db.execute('PRAGMA table_info(context_metadata)').fetchall()
        expected = [(0, 'key', 'TEXT', 1, None, 1), (1, 'value', 'TEXT', 1, None, 0)]
        if version != VERSION or columns != expected:
            raise RuntimeError('Invalid or unsupported local database schema.')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError('Local database integrity check failed.')
        if db.execute("SELECT value FROM context_metadata WHERE key='installation_id'").fetchone() != (installation_id,):
            raise RuntimeError('Database does not belong to this installation.')


def initialize_database(paths, installation_id):
    target = database_path(paths)
    if target.exists():
        validate_database(paths, installation_id)
        return
    safe_path(target)
    fd, temporary = tempfile.mkstemp(prefix='.schema-', dir=target.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.execute('CREATE TABLE context_metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)')
            db.execute('INSERT INTO context_metadata VALUES (?, ?)', ('installation_id', installation_id))
            db.execute(f'PRAGMA user_version = {VERSION}')
            db.commit()
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        # Publish the complete schema without overwriting an existing artifact.
        os.link(temporary, target)
        Path(temporary).unlink()
        sync_directory(target.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install():
    paths = locations()
    created = []
    # Validate every existing product directory before changing anything.
    for path in paths.values():
        safe_path(path)
        if path.exists():
            private(path, directory=True)
    for path in paths.values():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.mkdir(mode=0o700)
                created.append(str(path))
            except FileExistsError:
                pass
        private(path, directory=True)
    with locked(paths):
        path = manifest_path(paths)
        safe_path(path)
        if path.exists():
            manifest = read_manifest(paths)
            manifest['owned_directories'] = sorted(set(manifest['owned_directories'] + created))
        else:
            safe_path(database_path(paths))
            if database_path(paths).exists():
                raise RuntimeError('Unowned context.db already exists; preserving it.')
            manifest = {
                'schema_version': VERSION, 'product': 'bl-context',
                'installation_id': str(uuid.uuid4()), 'state': 'installing',
                'directories': {k: str(v) for k, v in paths.items()},
                'owned_directories': sorted(created),
                'owned_files': [str(database_path(paths)), str(path)],
                'interpreter': os.path.abspath(sys.executable),
                'executable': str(Path(sysconfig.get_path('scripts')) / 'blctx'),
            }
            atomic_manifest(paths, manifest)
        initialize_database(paths, manifest['installation_id'])
        manifest.update(state='active', interpreter=os.path.abspath(sys.executable),
                        executable=str(Path(sysconfig.get_path('scripts')) / 'blctx'))
        atomic_manifest(paths, manifest)


def verify():
    paths = locations()
    for path in paths.values():
        private(path, directory=True)
    manifest = read_manifest(paths)
    if manifest['state'] != 'active':
        raise RuntimeError('Context installation ownership is inactive.')
    for key in ('interpreter', 'executable'):
        path = Path(manifest[key])
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f'Recorded {key} is unavailable: {path}')
    validate_database(paths, manifest['installation_id'])
    return f'Private directories, schema v{VERSION}, and active ownership verified.'


def uninstall(purge=False):
    paths = locations()
    for path in paths.values():
        safe_path(path)
        if path.exists():
            private(path, directory=True)
    path = manifest_path(paths)
    safe_path(path)
    if not path.exists():
        return  # No ownership: never infer it from retained or unknown files.
    with locked(paths):
        manifest = read_manifest(paths)
        if purge and database_path(paths).exists():
            # A replacement database is not ours merely because its name matches.
            validate_database(paths, manifest['installation_id'])
        manifest['state'] = 'inactive'
        manifest.pop('embedding_model', None)
        manifest.pop('session_discovery', None)
        manifest.pop('installation_finalized', None)
        atomic_manifest(paths, manifest)
        if purge:
            purge_index_artifacts(paths, manifest)
            if manifest.get('sqlite_wal'):
                for suffix in ('-wal', '-shm'):
                    sidecar = Path(str(database_path(paths)) + suffix)
                    safe_path(sidecar)
                    if sidecar.exists():
                        private(sidecar)
                        sidecar.unlink()
            db = database_path(paths)
            safe_path(db)
            if db.exists():
                private(db)
                db.unlink()
            path.unlink()
            # Never recursively delete: unknown contents always survive.
            for directory in manifest['owned_directories']:
                try:
                    Path(directory).rmdir()
                except OSError:
                    pass


def verify_uninstalled():
    paths = locations()
    path = manifest_path(paths)
    safe_path(path)
    if path.exists() and read_manifest(paths)['state'] != 'inactive':
        raise RuntimeError('Context installation ownership is still active.')


def cancel_pending_index_jobs(paths=None):
    """An explicit uninstall disconnects durable work before it can replay."""
    paths = paths or locations()
    database = database_path(paths)
    if not database.exists():
        return 0
    with closing(sqlite3.connect(database, timeout=10)) as db, db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_jobs'"
        ).fetchone()
        if not exists:
            return 0
        return db.execute(
            "UPDATE ingest_jobs SET state='superseded' "
            "WHERE state IN ('queued','running')"
        ).rowcount


def prepare_index_artifacts(paths, kind):
    """Claim a new dedicated directory, never adopt unrelated existing files."""
    directory = paths['data'] / 'vectors' if kind == 'vectors' else paths['cache'] / 'embeddings'
    safe_path(directory)
    with locked(paths):
        manifest = read_manifest(paths)
        trees = manifest.setdefault('index_artifacts', {})
        if kind not in trees:
            if directory.exists():
                raise RuntimeError(f'Refusing unowned index directory: {directory}')
            trees[kind] = []
            atomic_manifest(paths, manifest)
        directory.mkdir(mode=0o700, exist_ok=True)
        private(directory, directory=True)
    return directory, {str(p.relative_to(directory)) for p in directory.rglob('*') if not p.is_dir()}


def record_index_artifacts(paths, kind, before):
    directory = paths['data'] / 'vectors' if kind == 'vectors' else paths['cache'] / 'embeddings'
    after = {str(p.relative_to(directory)) for p in directory.rglob('*') if not p.is_dir()}
    with locked(paths):
        manifest = read_manifest(paths)
        trees = manifest.setdefault('index_artifacts', {})
        trees[kind] = sorted(set(trees.get(kind, [])) | (after - before))
        atomic_manifest(paths, manifest)


def purge_index_artifacts(paths, manifest):
    for kind, entries in manifest.get('index_artifacts', {}).items():
        if kind not in ('vectors', 'embeddings') or not isinstance(entries, list):
            raise RuntimeError('Invalid index artifact ownership')
        directory = paths['data'] / 'vectors' if kind == 'vectors' else paths['cache'] / 'embeddings'
        safe_path(directory)
        owned_parents = {directory}
        for entry in entries:
            relative = Path(entry)
            if relative.is_absolute() or '..' in relative.parts or not relative.parts:
                raise RuntimeError('Invalid owned index artifact path')
            target = directory / relative
            parent = target.parent
            while parent != directory:
                owned_parents.add(parent)
                parent = parent.parent
            safe_path(target.parent)
            if target.is_file() or target.is_symlink():
                target.unlink()
        # Empty owned directory structure can be removed; unknown files survive.
        for target in sorted(owned_parents, key=lambda p: len(p.parts), reverse=True):
            if target.is_dir() and not target.is_symlink():
                try:
                    target.rmdir()
                except OSError:
                    pass
        try:
            directory.rmdir()
        except OSError:
            pass


def prepare_sqlite_wal(paths):
    """Reserve SQLite's standard WAL sidecars before changing journal mode."""
    with locked(paths):
        manifest = read_manifest(paths)
        for suffix in ('-wal', '-shm'):
            path = Path(str(database_path(paths)) + suffix)
            safe_path(path)
            if path.exists():
                if not manifest.get('sqlite_wal'):
                    raise RuntimeError(f'Refusing unowned SQLite sidecar: {path}')
                private(path)
        if not manifest.get('sqlite_wal'):
            manifest['sqlite_wal'] = True
            atomic_manifest(paths, manifest)
