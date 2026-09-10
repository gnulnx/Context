"""Pinned, private CPU embeddings; networking is confined to installation."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import hashlib
import os
import time

from . import storage

MODEL = 'BAAI/bge-small-en'
REPOSITORY = 'Qdrant/bge-small-en'
REVISION = '8791246cc2a79c7949a4dc0d4a018cbd7d024879'
DIMENSIONS = 384
# SHA256 of exact upstream bytes at REVISION (ONNX hash also published as LFS OID).
FILES = {
    'model_optimized.onnx': (132883455, '904dc556aacd699d056bcb46dec7535551ac876da69814baf0edc8fa0d184f4f'),
    'config.json': (701, '986feac5770b8861638179141c04a1535fa65f568d5f194b7c8a51145b5a6412'),
    'special_tokens_map.json': (125, 'b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3'),
    'tokenizer.json': (711396, 'd241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66'),
    'tokenizer_config.json': (366, '9261e7d79b44c8195c1cada2b453e55b00aeb81e907a6664974b4d7776172ab3'),
    'vocab.txt': (231508, '07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3'),
}
CONFIG = {'model': MODEL, 'repository': REPOSITORY, 'revision': REVISION, 'dimensions': DIMENSIONS}
progress_callback = ContextVar('embedding_progress', default=None)


def emit(stage, completed=0, total=0):
    callback = progress_callback.get()
    if callback:
        callback(stage, completed, total)


@contextmanager
def progress(console=None):
    """CLI-only Rich display; JSON and library callers remain silent."""
    if console is None:
        yield
        return
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, DownloadColumn, TimeRemainingColumn
    display = Progress(SpinnerColumn(), TextColumn('{task.description}'), BarColumn(),
                       DownloadColumn(), TimeRemainingColumn(), console=console)
    task = None
    task_total = None
    previous_stage = None
    def update(stage, completed, total):
        nonlocal task, task_total, previous_stage
        if not console.is_terminal:
            if stage != previous_stage:
                console.print(f' {stage}…', markup=False)
                previous_stage = stage
            return
        if task is None:
            task = display.add_task(stage, total=total or None, completed=completed)
            task_total = total
            display.start()
        elif task_total != total:
            display.remove_task(task)
            task = display.add_task(stage, total=total or None, completed=completed)
            task_total = total
        else:
            display.update(task, description=stage, completed=completed)
    token = progress_callback.set(update)
    try:
        yield
    finally:
        if task is not None:
            display.stop()
        progress_callback.reset(token)


def directory(paths):
    return paths['cache'] / 'embeddings' / REVISION


def cache_lock(paths):
    """Serialize downloads and purge without holding the hook receipt lock."""
    root = paths['cache'] / 'embeddings'
    if not storage.manifest_path(paths).exists():
        return nullcontext()
    if 'embeddings' not in storage.read_manifest(paths).get('index_artifacts', {}):
        return nullcontext()
    storage.safe_path(root)
    return storage.locked({'state': root}) if root.exists() else nullcontext()


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def valid(path, size, sha):
    storage.safe_path(path)
    if not path.exists():
        return False
    storage.private(path)
    return path.stat().st_size == size and digest(path) == sha


def download(path, size, sha, report):
    """Resume exact revision bytes; publish only after size and SHA256 match."""
    import requests
    partial = path.with_suffix(path.suffix + '.part')
    storage.safe_path(partial)
    if partial.exists():
        storage.private(partial)
    else:
        partial.touch(mode=0o600, exist_ok=False)
    for attempt in range(3):
        offset = partial.stat().st_size
        if offset >= size:
            if offset == size and digest(partial) == sha:
                os.replace(partial, path)
                report(size)
                return
            partial.write_bytes(b'')
            offset = 0
        report(offset)
        try:
            with requests.get(f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{path.name}',
                              headers={'Range': f'bytes={offset}-', 'Accept-Encoding': 'identity'},
                              stream=True, timeout=(10, 30)) as response:
                if response.status_code == 429:
                    # Do not hammer the host or hide a five-minute wait in onboarding.
                    retry = response.headers.get('Retry-After') or response.headers.get('RateLimit', 'later')
                    raise RuntimeError(f'Model host rate limited this IP. Retry {retry}; downloaded bytes are retained.')
                response.raise_for_status()
                if response.status_code == 206:
                    expected = f'bytes {offset}-{size-1}/{size}'
                    if response.headers.get('Content-Range') != expected:
                        raise RuntimeError('Model host returned an unexpected byte range; retry installation.')
                elif response.status_code == 200:
                    offset = 0  # Server ignored Range: safely restart, never append twice.
                else:
                    raise RuntimeError(f'Unexpected model download status: {response.status_code}')
                with partial.open('ab' if offset else 'wb') as stream:
                    for block in response.iter_content(256 * 1024):
                        if offset + len(block) > size:
                            raise RuntimeError('Model download exceeded its pinned size.')
                        stream.write(block)
                        offset += len(block)
                        report(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
            if offset != size:
                raise requests.ConnectionError('Model download ended early')
            if digest(partial) != sha:
                partial.write_bytes(b'')
                raise RuntimeError('Model checksum failed; rerun installation to download a fresh copy.')
            os.replace(partial, path)
            storage.sync_directory(path.parent)
            return
        except requests.RequestException:
            if attempt == 2:
                raise RuntimeError('Model download interrupted. Rerun blctx install codex --step embedding_model to resume.') from None
            time.sleep(2 ** attempt)


def load_files(paths):
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL, cache_dir=str(directory(paths)),
                         specific_model_path=str(directory(paths)),
                         local_files_only=True, threads=2, cuda=False)


def probe(model):
    import numpy as np
    documents = ['The rover uses lithium batteries for electrical power.',
                 'The web interface has a purple navigation sidebar.',
                 'Database backups run nightly and retain seven snapshots.']
    queries = ['rover electrical power batteries', 'website navigation appearance', 'nightly database backup retention']
    passages = np.array(list(model.passage_embed(documents)))
    queries = np.array(list(model.query_embed(queries)))
    for values in (passages, queries):
        if values.shape != (3, DIMENSIONS) or not np.isfinite(values).all() or not (np.linalg.norm(values, axis=1) > 0).all():
            raise RuntimeError('Embedding probe returned invalid shape, nonfinite values, or zero vectors.')
    if not np.array_equal((queries @ passages.T).argmax(axis=1), np.arange(3)):
        raise RuntimeError('Embedding retrieval probe failed.')


def require_active(paths):
    manifest = storage.read_manifest(paths)
    if manifest['state'] != 'active' or manifest.get('embedding_model') != CONFIG:
        raise RuntimeError('Embedding model is not configured. Run blctx install codex --step embedding_model.')


def check_files(paths):
    for name, (size, sha) in FILES.items():
        if not valid(directory(paths) / name, size, sha):
            raise RuntimeError(f'Pinned model file missing or damaged: {name}. Rerun blctx install codex --step embedding_model.')


def load(paths):
    require_active(paths)
    check_files(paths)
    return load_files(paths)


def install():
    paths = storage.locations()
    storage.verify()
    root, _ = storage.prepare_index_artifacts(paths, 'embeddings')
    with cache_lock(paths):
        target = directory(paths)
        storage.safe_path(target)
        with storage.locked(paths):
            manifest = storage.read_manifest(paths)
            if manifest['state'] != 'active':
                raise RuntimeError('Context installation is inactive.')
            # Reserve exact files before writing so interrupted work remains purgeable.
            entries = manifest['index_artifacts']['embeddings']
            owned = [f'{REVISION}/{name}{suffix}' for name in FILES for suffix in ('', '.part')]
            for name in owned:
                path = root / name
                storage.safe_path(path)
                if path.exists() and name not in entries:
                    raise RuntimeError(f'Refusing unowned model file: {path}')
            manifest.pop('embedding_model', None)
            manifest['index_artifacts']['embeddings'] = sorted(set(entries + owned))
            storage.atomic_manifest(paths, manifest)
        target.mkdir(mode=0o700, exist_ok=True)
        storage.private(target, directory=True)
        total = sum(size for size, _ in FILES.values())
        completed = 0
        for name, (size, sha) in FILES.items():
            emit('Checking cached model', completed, total)
            path = target / name
            if not valid(path, size, sha):
                if os.environ.get('HF_HUB_OFFLINE', '').upper() in ('1', 'TRUE', 'YES', 'ON'):
                    raise RuntimeError('Model cache incomplete and HF_HUB_OFFLINE is set. Retry online to finish the download.')
                download(path, size, sha, lambda n: emit('Downloading embedding model', completed + n, total))
            completed += size
        emit('Loading model and verifying retrieval')
        probe(load_files(paths))
        with storage.locked(paths):
            manifest = storage.read_manifest(paths)
            if manifest['state'] != 'active':
                raise RuntimeError('Context installation was disconnected during model setup.')
            manifest['embedding_model'] = CONFIG
            storage.atomic_manifest(paths, manifest)
        emit('Embedding model verified', total, total)


def verify():
    paths = storage.locations()
    storage.verify()
    with cache_lock(paths):
        probe(load(paths))
    return f'{MODEL}: pinned revision {REVISION[:12]}, CPU, 384 dimensions; finite embeddings and retrieval verified offline.'
