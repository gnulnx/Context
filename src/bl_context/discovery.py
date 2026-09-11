"""Fast, metadata-only discovery of local Codex session files."""

import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from . import storage

PROVIDER = "openai/codex"
RECENT_SESSION_LIMIT = 5
SESSION_DIRECTORIES = ("sessions", "archived_sessions")


def codex_root():
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().absolute() if configured else Path.home() / ".codex"


def scan(base=None):
    """Return a compact inventory without opening transcript contents."""
    base = (base or codex_root()).absolute()
    storage.safe_path(base)
    candidates = []
    counts = {"active": 0, "archived": 0}

    def raise_walk_error(error):
        raise error

    for directory_name in SESSION_DIRECTORIES:
        directory = base / directory_name
        storage.safe_path(directory)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise RuntimeError(f"Codex history path is not a directory: {directory}")
        archived = directory_name == "archived_sessions"
        kind = "archived" if archived else "active"
        for parent, directories, filenames in os.walk(
            directory, followlinks=False, onerror=raise_walk_error
        ):
            directories[:] = [
                name
                for name in directories
                if not (Path(parent) / name).is_symlink()
            ]
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = Path(parent) / filename
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                counts[kind] += 1
                candidates.append(
                    {
                        "path": str(path.absolute()),
                        "archived": archived,
                        "size": info.st_size,
                        "mtime_ns": info.st_mtime_ns,
                    }
                )

    candidates.sort(key=lambda item: (-item["mtime_ns"], item["path"]))
    selected = candidates[:RECENT_SESSION_LIMIT]
    return {
        "schema_version": 1,
        "provider": PROVIDER,
        "root": str(base),
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "sessions": len(candidates),
        "active": counts["active"],
        "archived": counts["archived"],
        "selected_for_indexing": selected,
    }


def install():
    paths = storage.locations()
    storage.verify()
    report = scan()
    with storage.locked(paths):
        manifest = storage.read_manifest(paths)
        if manifest["state"] != "active":
            raise RuntimeError("Context installation is inactive.")
        manifest["session_discovery"] = report
        storage.atomic_manifest(paths, manifest)


def report(paths=None):
    paths = paths or storage.locations()
    storage.verify()
    inventory = storage.read_manifest(paths).get("session_discovery")
    if not inventory:
        raise RuntimeError(
            "Codex sessions have not been discovered. "
            "Run blctx install codex --step session_discovery."
        )
    if (
        inventory.get("schema_version") != 1
        or inventory.get("provider") != PROVIDER
        or inventory.get("root") != str(codex_root().absolute())
    ):
        raise RuntimeError("Codex session discovery belongs to another provider or root.")
    sessions = inventory.get("sessions")
    active = inventory.get("active")
    archived = inventory.get("archived")
    selected = inventory.get("selected_for_indexing")
    if (
        type(sessions) is not int
        or sessions < 0
        or type(active) is not int
        or active < 0
        or type(archived) is not int
        or archived < 0
        or active + archived != sessions
        or not isinstance(selected, list)
        or len(selected) != min(RECENT_SESSION_LIMIT, sessions)
    ):
        raise RuntimeError("Invalid Codex session discovery report.")
    root = Path(inventory["root"])
    seen = set()
    for item in selected:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid selected Codex session.")
        path = Path(item.get("path", ""))
        archived = item.get("archived")
        expected_directory = root / (
            "archived_sessions" if archived is True else "sessions"
        )
        if (
            not path.is_absolute()
            or path.suffix != ".jsonl"
            or path in seen
            or expected_directory not in path.parents
            or type(item.get("size")) is not int
            or type(item.get("mtime_ns")) is not int
            or not isinstance(archived, bool)
        ):
            raise RuntimeError("Invalid selected Codex session.")
        seen.add(path)
    return inventory


def selected_sessions(paths=None):
    """Return the five recent snapshots reserved for the indexing step."""
    return tuple(item.copy() for item in report(paths)["selected_for_indexing"])


def verify():
    inventory = report()
    selected = len(inventory["selected_for_indexing"])
    selection = (
        f"{selected} most recent selected for indexing."
        if selected
        else "no sessions selected for indexing."
    )
    return f"{inventory['sessions']} Codex sessions discovered; {selection}"
