"""Install the small, recent-history index selected during discovery."""

import hashlib
import stat
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from . import discovery
from .retrieval_cli import call

POLL_INTERVAL = 0.25
INSTALL_TIMEOUT = 600
progress_callback = ContextVar("history_index_progress", default=None)


def emit(stage, completed=0, total=0):
    callback = progress_callback.get()
    if callback:
        callback(stage, completed, total)


@contextmanager
def progress(callback=None):
    """Publish indexing progress into the installer's history row."""
    if callback is None:
        yield
        return
    token = progress_callback.set(callback)
    try:
        yield
    finally:
        progress_callback.reset(token)


def install():
    selected = discovery.selected_sessions()
    total = len(selected)
    if not selected:
        emit("No recent sessions to index", 0, 0)
        return

    emit("Indexing recent sessions", 0, total)
    status = call({"operation": "index_status", "details": True})
    indexed = {item["path"]: item for item in status.get("sources", [])}
    pending = [item["path"] for item in selected if not source_is_valid(item, indexed.get(item["path"]))]
    already_indexed = total - len(pending)
    emit("Indexing recent sessions", already_indexed, total)
    if not pending:
        emit("Recent sessions indexed", total, total)
        return

    job = call({"operation": "index", "sources": pending})
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Indexer did not return a durable job ID.")

    deadline = time.monotonic() + INSTALL_TIMEOUT
    while time.monotonic() < deadline:
        job = call({"operation": "index_status", "job_id": job_id, "details": True})
        result = job.get("result", {})
        completed = already_indexed + len(result.get("files", [])) + len(
            result.get("failed", [])
        )
        emit("Indexing recent sessions", min(completed, total), total)
        if job.get("state") not in ("queued", "running"):
            break
        time.sleep(POLL_INTERVAL)
    else:
        raise RuntimeError(
            f"Recent-session indexing exceeded {INSTALL_TIMEOUT // 60} minutes; "
            f"durable job {job_id} is still running."
        )

    result = job.get("result", {})
    failed = result.get("failed", [])
    indexed = result.get("files", [])
    if (
        failed
        or len(indexed) != len(pending)
        or job.get("state") not in ("complete", "partial")
    ):
        details = "; ".join(item.get("error", "unknown error") for item in failed)
        raise RuntimeError(
            f"Indexed {already_indexed + len(indexed)} of {total} recent sessions"
            + (f": {details}" if details else f"; job state is {job.get('state')}")
        )
    emit("Recent sessions indexed", total, total)


def source_is_valid(snapshot, source):
    if not isinstance(source, dict):
        return False
    source_report = source.get("report")
    try:
        path = Path(snapshot["path"])
        path_info = path.stat(follow_symlinks=False)
        indexed_bytes = source.get("bytes")
        digest = source.get("digest")
        if (
            not isinstance(source_report, dict)
            or not stat.S_ISREG(path_info.st_mode)
            or type(indexed_bytes) is not int
            or indexed_bytes < 0
            or path_info.st_size < indexed_bytes
            or not isinstance(digest, str)
        ):
            return False
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            remaining = indexed_bytes
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    return False
                checksum.update(block)
                remaining -= len(block)
        return checksum.hexdigest() == digest
    except (OSError, TypeError, ValueError):
        return False


def report():
    selected = discovery.selected_sessions()
    if not selected:
        return {"sessions": 0, "messages": 0, "chunks": 0, "unclassified": 0}

    status = call({"operation": "index_status", "details": True})
    indexed = {item["path"]: item for item in status.get("sources", [])}
    reports = []
    growing = 0
    for snapshot in selected:
        source = indexed.get(snapshot["path"])
        if not source_is_valid(snapshot, source):
            raise RuntimeError(
                "A selected recent Codex session is missing or changed since indexing."
            )
        source_report = source["report"]
        if source["bytes"] < snapshot["size"]:
            growing += 1
        reports.append(source_report)
    return {
        "sessions": len(reports),
        "messages": sum(item.get("messages", 0) for item in reports),
        "chunks": sum(item.get("chunks", 0) for item in reports),
        "unclassified": sum(item.get("unclassified", 0) for item in reports),
        "growing": growing,
    }


def verify():
    result = report()
    if result["sessions"] == 0:
        return "No Codex sessions available to index."
    summary = (
        f"{result['sessions']} most recent Codex sessions indexed; "
        f"{result['messages']} messages and {result['chunks']} searchable chunks."
    )
    if result["unclassified"]:
        summary += f" {result['unclassified']} unsupported records skipped."
    if result["growing"]:
        summary += f" {result['growing']} live append-only session has newer content."
    return summary
