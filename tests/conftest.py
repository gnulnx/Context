import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from bl_context import embedding, launchd_service, storage


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Darwin's default pytest paths exceed its 104-byte Unix socket limit even
    # before appending the native Application Support state directory.
    if sys.platform == 'darwin':
        with tempfile.TemporaryDirectory(prefix='blct-', dir='/private/tmp') as directory:
            yield Path(directory)
    else:
        yield tmp_path_factory.mktemp('context')


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    for kind in ('DATA', 'CONFIG', 'CACHE', 'STATE'):
        monkeypatch.delenv(f'XDG_{kind}_HOME', raising=False)
        monkeypatch.delenv(f'BLCTX_{kind}_DIR', raising=False)
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/nonexistent/blctx-test-bus')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / 'runtime'))
    if os.environ.get('BLCTX_LAUNCHD_TEST') != '1':
        def unavailable(*args, **kwargs):
            raise RuntimeError('launchd unavailable in isolated unit tests')
        monkeypatch.setattr(launchd_service, 'manager', unavailable)


@pytest.fixture
def install_embedding(monkeypatch):
    """Install real pinned bytes only for explicitly enabled integration tests."""
    def install():
        paths = storage.locations()
        cache = Path(
            os.environ.get('BLCTX_MODEL_TEST_CACHE', '/tmp/context-embedding-test-cache')
        ) / embedding.REVISION
        if cache.exists():
            root, before = storage.prepare_index_artifacts(paths, 'embeddings')
            shutil.copytree(cache, root / embedding.REVISION, dirs_exist_ok=True)
            storage.record_index_artifacts(paths, 'embeddings', before)
        else:
            monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
        embedding.install()

    return install
