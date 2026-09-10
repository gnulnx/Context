"""Download recovery over real HTTP and opt-in actual CPU model lifecycle."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest
import requests
from click.testing import CliRunner

from bl_context import embedding, storage
from bl_context.cli import main


@pytest.fixture
def host(monkeypatch):
    payload = b'model bytes\n' * 100000
    state = {'ranges': [], 'mode': 'interrupt'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            offset = int(self.headers['Range'].split('=')[1].split('-')[0])
            state['ranges'].append(offset)
            if state['mode'] == 'limited':
                self.send_response(429)
                self.send_header('Retry-After', '300')
                self.end_headers()
                return
            if state['mode'] == 'restart':
                offset = 0
                self.send_response(200)
            else:
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {offset}-{len(payload)-1}/{len(payload)}')
            self.send_header('Content-Length', str(len(payload)-offset))
            self.end_headers()
            data = payload[offset:]
            if state['mode'] == 'interrupt' and len(state['ranges']) == 1:
                data = data[:400000]
                self.close_connection = True
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    get = requests.get
    monkeypatch.setattr(requests, 'get', lambda url, **kw: get(f'http://127.0.0.1:{server.server_port}/model', **kw))
    try:
        yield state, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_interrupted_http_download_resumes_and_checks_hash(tmp_path, host):
    state, payload = host
    target = tmp_path/'model.onnx'
    updates = []
    embedding.download(target, len(payload), hashlib.sha256(payload).hexdigest(), updates.append)
    assert state['ranges'][0] == 0 and state['ranges'][1] > 0
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o777 == 0o600
    assert updates[-1] == len(payload)
    assert not target.with_suffix('.onnx.part').exists()


def test_range_ignored_and_rate_limit_preserves_partial(tmp_path, host):
    state, payload = host
    target = tmp_path/'model.onnx'
    partial = target.with_suffix('.onnx.part')
    partial.touch(mode=0o600)
    partial.write_bytes(payload[:100])
    state['mode'] = 'limited'
    with pytest.raises(RuntimeError, match='rate limited'):
        embedding.download(target, len(payload), hashlib.sha256(payload).hexdigest(), lambda n: None)
    assert state['ranges'] == [100] and partial.read_bytes() == payload[:100]
    state['mode'] = 'restart'
    embedding.download(target, len(payload), hashlib.sha256(payload).hexdigest(), lambda n: None)
    assert target.read_bytes() == payload


def test_checksum_failure_never_publishes(tmp_path, host):
    state, payload = host
    state['mode'] = 'restart'
    with pytest.raises(RuntimeError, match='checksum'):
        embedding.download(tmp_path/'model.onnx', len(payload), '0'*64, lambda n: None)
    assert not (tmp_path/'model.onnx').exists()


def test_unconfigured_model_never_downloads_and_uninstall_clears_selection(monkeypatch):
    storage.install()
    paths = storage.locations()
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: pytest.fail('Unexpected network'))
    with pytest.raises(RuntimeError, match='not configured'):
        embedding.load(paths)
    manifest = storage.read_manifest(paths)
    manifest['embedding_model'] = embedding.CONFIG
    storage.atomic_manifest(paths, manifest)
    storage.uninstall()
    storage.install()
    with pytest.raises(RuntimeError, match='not configured'):
        embedding.require_active(paths)


def test_partial_setup_is_owned_and_purge_preserves_unknown(monkeypatch):
    storage.install()
    paths = storage.locations()
    with pytest.raises(RuntimeError, match='OFFLINE'):
        embedding.install()
    root = embedding.directory(paths)
    unknown = root/'user-note'
    unknown.write_text('keep')
    partial = root/'model_optimized.onnx.part'
    partial.write_bytes(b'interrupted')
    storage.uninstall(purge=True)
    assert unknown.read_text() == 'keep'
    assert not partial.exists()


def test_model_refuses_unowned_file_and_symlink(tmp_path):
    storage.install()
    paths = storage.locations()
    root, _ = storage.prepare_index_artifacts(paths, 'embeddings')
    target = root/embedding.REVISION
    target.mkdir(mode=0o700)
    victim = target/'config.json'
    victim.write_text('user file')
    with pytest.raises(RuntimeError, match='unowned'):
        embedding.install()
    victim.unlink()
    victim.symlink_to(tmp_path/'elsewhere')
    with pytest.raises(RuntimeError, match='symbolic link'):
        embedding.install()


@pytest.mark.skipif(os.environ.get('BLCTX_INDEX_TEST') != '1', reason='Actual pinned CPU model')
def test_real_model_offline_lifecycle_and_corruption(tmp_path, install_embedding, monkeypatch):
    monkeypatch.setenv('FASTEMBED_CACHE_PATH', str(tmp_path/'unrelated-global-cache'))
    storage.install()
    install_embedding()
    assert not (tmp_path/'unrelated-global-cache').exists()
    paths = storage.locations()
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: pytest.fail('Offline lifecycle contacted network'))
    runner = CliRunner()
    def call(args, code):
        result = runner.invoke(main, args + ['--json'])
        assert result.exit_code == code, result.output
        assert json.loads(result.output)['exit_code'] == code
    for command in ['status', 'doctor']:
        call([command, '--step', 'embedding_model'], 0)
    before = {p.name:p.stat().st_mtime_ns for p in embedding.directory(paths).iterdir()}
    call(['uninstall','codex'], 0)
    for command in ['status', 'doctor']:
        call([command, '--step', 'embedding_model'], 1)
    call(['install','codex','--step','embedding_model'], 0)
    assert before == {p.name:p.stat().st_mtime_ns for p in embedding.directory(paths).iterdir()}
    bad = embedding.directory(paths)/'config.json'
    original = bad.read_bytes()
    bad.write_bytes(b'damaged')
    call(['doctor','--step','embedding_model'], 1)
    bad.write_bytes(original)
    call(['doctor','--step','embedding_model'], 0)
    call(['uninstall','codex','--purge'], 0)
    assert not embedding.directory(paths).exists()


def test_uninstall_preserves_unowned_cache_tree():
    storage.install()
    paths = storage.locations()
    root = paths['cache']/'embeddings'
    root.mkdir(mode=0o755)
    (root/'user-file').write_text('preserve')
    storage.uninstall(purge=True)
    assert (root/'user-file').read_text() == 'preserve'


@pytest.mark.parametrize('failure', ['shape', 'nonfinite', 'zero', 'ranking'])
def test_probe_rejects_unusable_vectors(failure):
    import numpy as np
    class Broken:
        def passage_embed(self, texts):
            values = np.zeros((3, embedding.DIMENSIONS))
            values[:, :3] = np.eye(3)
            if failure == 'shape': return values[:, :10]
            if failure == 'nonfinite': values[0, 0] = np.nan
            if failure == 'zero': values[0] = 0
            if failure == 'ranking': values = values[::-1]
            return values
        def query_embed(self, texts):
            values = np.zeros((3, embedding.DIMENSIONS))
            values[:, :3] = np.eye(3)
            return values
    with pytest.raises(RuntimeError):
        embedding.probe(Broken())
