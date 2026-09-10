import pytest


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for kind in ('DATA', 'CONFIG', 'CACHE', 'STATE'):
        monkeypatch.delenv(f'XDG_{kind}_HOME', raising=False)
