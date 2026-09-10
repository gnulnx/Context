import pytest


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    for kind in ('DATA', 'CONFIG', 'CACHE', 'STATE'):
        monkeypatch.delenv(f'XDG_{kind}_HOME', raising=False)
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/nonexistent/blctx-test-bus')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / 'runtime'))


@pytest.fixture
def install_embedding(monkeypatch):
    """Opt-in integration tests use real pinned bytes; ordinary tests stay offline."""
    def install():
        import os
        import shutil
        from pathlib import Path
        from bl_context import embedding, storage
        paths = storage.locations()
        cache = Path(os.environ.get('BLCTX_MODEL_TEST_CACHE', '/tmp/context-embedding-test-cache')) / embedding.REVISION
        if cache.exists():
            root, before = storage.prepare_index_artifacts(paths, 'embeddings')
            shutil.copytree(cache, root / embedding.REVISION, dirs_exist_ok=True)
            storage.record_index_artifacts(paths, 'embeddings', before)
        else:
            monkeypatch.delenv('HF_HUB_OFFLINE', raising=False)
        embedding.install()
    return install
