import hashlib

import pytest

from bl_context import history_index


def test_install_indexes_selected_sessions_in_recency_order(monkeypatch):
    selected = [
        {"path": "/sessions/newest.jsonl"},
        {"path": "/sessions/newer.jsonl"},
    ]
    statuses = [
        {"state": "running", "result": {"files": [], "failed": []}},
        {
            "state": "running",
            "result": {"files": [{"path": selected[0]["path"]}], "failed": []},
        },
        {
            "state": "complete",
            "result": {"files": selected, "failed": []},
        },
    ]
    requests = []
    updates = []

    def call(request):
        requests.append(request)
        if request["operation"] == "index":
            return {"job_id": "job-1", "state": "queued"}
        if "job_id" not in request:
            return {"sources": []}
        return statuses.pop(0)

    monkeypatch.setattr(history_index.discovery, "selected_sessions", lambda: selected)
    monkeypatch.setattr(history_index, "call", call)
    monkeypatch.setattr(history_index.time, "sleep", lambda _: None)

    with history_index.progress(lambda *update: updates.append(update)):
        history_index.install()

    assert next(item for item in requests if item["operation"] == "index") == {
        "operation": "index",
        "sources": ["/sessions/newest.jsonl", "/sessions/newer.jsonl"],
    }
    assert updates[-1] == ("Recent sessions indexed", 2, 2)


def test_install_rejects_failed_sources(monkeypatch):
    monkeypatch.setattr(
        history_index.discovery,
        "selected_sessions",
        lambda: [{"path": "/sessions/recent.jsonl"}],
    )

    def call(request):
        if request["operation"] == "index":
            return {"job_id": "job-1", "state": "queued"}
        return {
            "state": "partial",
            "result": {"files": [], "failed": [{"error": "invalid transcript"}]},
        }

    monkeypatch.setattr(history_index, "call", call)

    with pytest.raises(RuntimeError, match="invalid transcript"):
        history_index.install()


def test_install_reuses_a_valid_indexed_prefix(monkeypatch, tmp_path):
    path = tmp_path / "live.jsonl"
    path.write_text("indexed prefix\nnew content\n")
    prefix = b"indexed prefix\n"
    selected = [{"path": str(path), "size": path.stat().st_size}]
    requests = []

    def call(request):
        requests.append(request)
        return {
            "sources": [
                {
                    "path": str(path),
                    "bytes": len(prefix),
                    "digest": hashlib.sha256(prefix).hexdigest(),
                    "report": {},
                }
            ]
        }

    monkeypatch.setattr(history_index.discovery, "selected_sessions", lambda: selected)
    monkeypatch.setattr(history_index, "call", call)

    history_index.install()

    assert requests == [{"operation": "index_status", "details": True}]


def test_install_indexes_an_existing_session_missing_from_the_index(monkeypatch, tmp_path):
    path = tmp_path / "recent.jsonl"
    path.write_text("session history\n")
    selected = [{"path": str(path), "size": path.stat().st_size}]
    requests = []

    def call(request):
        requests.append(request)
        if request == {"operation": "index_status", "details": True}:
            return {"sources": []}
        if request["operation"] == "index":
            return {"job_id": "job-1", "state": "queued"}
        return {
            "state": "complete",
            "result": {"files": selected, "failed": []},
        }

    monkeypatch.setattr(history_index.discovery, "selected_sessions", lambda: selected)
    monkeypatch.setattr(history_index, "call", call)

    history_index.install()

    assert {"operation": "index", "sources": [str(path)]} in requests


def test_verify_accepts_append_only_growth_after_discovery(monkeypatch, tmp_path):
    path = tmp_path / "recent.jsonl"
    path.write_text("history\n")
    info = path.stat()
    indexed_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    selected = [
        {"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    ]
    monkeypatch.setattr(history_index.discovery, "selected_sessions", lambda: selected)
    monkeypatch.setattr(
        history_index,
        "call",
        lambda _: {
            "sources": [
                {
                    "path": selected[0]["path"],
                    "missing": False,
                    "changed_since_index": True,
                    "bytes": info.st_size,
                    "digest": indexed_digest,
                    "report": {
                        "messages": 4,
                        "chunks": 6,
                        "unclassified": 2,
                        "source_mtime_ns": info.st_mtime_ns,
                    },
                }
            ]
        },
    )

    path.write_text("history\nnew live content\n")
    assert history_index.verify() == (
        "1 most recent Codex sessions indexed; 4 messages and 6 searchable chunks. "
        "2 unsupported records skipped."
    )

    path.write_text("")
    with pytest.raises(RuntimeError, match="missing or changed"):
        history_index.verify()


def test_empty_discovery_requires_no_daemon_call(monkeypatch):
    monkeypatch.setattr(history_index.discovery, "selected_sessions", lambda: ())
    monkeypatch.setattr(
        history_index,
        "call",
        lambda _: pytest.fail("empty discovery should not contact the daemon"),
    )

    history_index.install()

    assert history_index.verify() == "No Codex sessions available to index."
