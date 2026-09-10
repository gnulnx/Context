import pytest


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    for kind in ('DATA', 'CONFIG', 'CACHE', 'STATE'):
        monkeypatch.delenv(f'XDG_{kind}_HOME', raising=False)
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/nonexistent/blctx-test-bus')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path / 'runtime'))
