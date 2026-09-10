"""Package-backed skill installation with hash-based ownership and upgrades."""
import hashlib
from importlib.resources import files
import os
from pathlib import Path
import tempfile

from . import storage

SKILL_NAME = 'base-layer-context'
SKILL_VERSION = '0.2.0'


def source():
    return files('bl_context').joinpath('skills', SKILL_NAME, 'SKILL.md').read_text(encoding='utf-8')


def root():
    return Path(os.path.abspath(Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser()))


def target(directory=None):
    return (directory or root())/'skills'/SKILL_NAME/'SKILL.md'


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def owned_path(owned):
    directory = Path(owned.get('root', ''))
    path = Path(owned.get('path', ''))
    if (not directory.is_absolute() or path != target(directory)
            or owned.get('name') != SKILL_NAME
            or not isinstance(owned.get('sha256'), str) or len(owned['sha256']) != 64):
        raise RuntimeError('Invalid skill ownership path or hash; preserving files')
    storage.safe_path(path)
    return path


def check_duplicates():
    # Do not populate a second user discovery root, including a custom CODEX_HOME.
    for directory in (Path.home()/'.agents', Path.home()/'.codex'):
        candidate = target(directory)
        if candidate != target() and (candidate.parent.exists() or candidate.parent.is_symlink()):
            raise RuntimeError(f'Another Context skill exists in a user discovery root: {candidate.parent}')


def installed_digest(path):
    storage.private(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install():
    skill_text = source()
    storage.verify()
    paths = storage.locations()
    path = target()
    storage.safe_path(path)
    check_duplicates()
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        owned = manifest.get('codex_skill')
        if owned and owned_path(owned) != path:
            raise RuntimeError('Skill belongs to another CODEX_HOME; uninstall that installation first')
        if path.exists():
            if not owned or installed_digest(path) != owned['sha256']:
                raise RuntimeError(f'Codex skill collision or modified file; preserving it: {path}')
        elif not owned and path.parent.exists():
            raise RuntimeError(f'Codex skill directory already exists; preserving it: {path.parent}')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        new_owned = {'root':str(root()), 'path':str(path), 'name':SKILL_NAME,
                     'version':SKILL_VERSION, 'sha256':digest(skill_text)}
        if not path.exists() or installed_digest(path) != new_owned['sha256']:
            fd, temporary = tempfile.mkstemp(prefix='.skill-', dir=path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(skill_text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                storage.sync_directory(path.parent)
            finally:
                Path(temporary).unlink(missing_ok=True)
        manifest['codex_skill'] = new_owned
        storage.atomic_manifest(paths, manifest)
    verify()


def verify():
    storage.verify()
    owned = storage.read_manifest(storage.locations()).get('codex_skill')
    if not owned:
        raise RuntimeError('Context Codex skill is not installed; run blctx install codex --step codex_skills')
    path = owned_path(owned)
    if path != target():
        raise RuntimeError('Current CODEX_HOME differs from the owned skill root')
    check_duplicates()
    from .mcp_registration import config
    settings = config(root()).get('skills', {}).get('config', [])
    if any(entry.get('enabled') is False and entry.get('path') == str(path) for entry in settings):
        raise RuntimeError('Context skill is disabled in Codex configuration; preserving that preference')
    if (owned.get('version') != SKILL_VERSION or owned['sha256'] != digest(source())
            or not path.is_file() or installed_digest(path) != owned['sha256']):
        raise RuntimeError('Context skill is missing, modified, or outdated; install the current version')
    return f'Codex skill {SKILL_NAME} v{SKILL_VERSION} content and ownership verified.'


def uninstall():
    paths = storage.locations()
    if not storage.manifest_path(paths).exists():
        return
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        owned = manifest.get('codex_skill')
        if not owned:
            return
        path = owned_path(owned)
        if path.exists():
            if installed_digest(path) != owned['sha256']:
                raise RuntimeError('Owned Codex skill was modified; refusing to remove it')
            path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass  # Additional user files survive uninstall.
        manifest.pop('codex_skill')
        storage.atomic_manifest(paths, manifest)
