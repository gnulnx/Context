import os
import time
from pathlib import Path

from click.testing import CliRunner

from bl_context import discovery, storage
from bl_context.cli import main


def create_session(path, mtime_ns):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not parsed during discovery\n")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_discovery_reports_total_and_selects_five_most_recent():
    root = discovery.codex_root()
    files = []
    for index in range(7):
        path = root / "sessions" / "2026" / f"active-{index}.jsonl"
        create_session(path, index + 1)
        files.append(path)
    for index in range(2):
        path = root / "archived_sessions" / f"archived-{index}.jsonl"
        create_session(path, index + 20)
        files.append(path)

    storage.install()
    discovery.install()

    inventory = discovery.report()
    assert inventory["sessions"] == 9
    assert inventory["active"] == 7
    assert inventory["archived"] == 2
    assert [Path(item["path"]) for item in discovery.selected_sessions()] == sorted(
        files, key=lambda path: (-path.stat().st_mtime_ns, str(path))
    )[:5]
    assert discovery.verify() == (
        "9 Codex sessions discovered; 5 most recent selected for indexing."
    )


def test_discovery_cli_reports_without_indexing():
    root = discovery.codex_root()
    for index in range(6):
        create_session(root / "sessions" / f"session-{index}.jsonl", index + 1)

    result = CliRunner().invoke(
        main,
        ["install", "codex", "--step", "session_discovery", "--no-color"],
    )

    assert result.exit_code == 0
    assert "6 Codex sessions discovered; 5 most recent selected for indexing." in result.output
    manifest = storage.read_manifest(storage.locations())
    assert "history_index" not in manifest


def test_discovery_does_not_change_existing_index_state():
    storage.install()
    paths = storage.locations()
    manifest = storage.read_manifest(paths)
    manifest["history_index"] = {"completed": 3}
    storage.atomic_manifest(paths, manifest)

    discovery.install()

    assert storage.read_manifest(paths)["history_index"] == {"completed": 3}


def test_discovery_ignores_symlinks_and_non_jsonl(tmp_path):
    root = discovery.codex_root()
    create_session(root / "sessions" / "valid.jsonl", 1)
    (root / "sessions" / "notes.txt").write_text("ignore")
    (root / "sessions" / "linked.jsonl").symlink_to(tmp_path / "outside.jsonl")
    (root / "sessions" / "linked-directory").symlink_to(tmp_path, target_is_directory=True)

    inventory = discovery.scan()

    assert inventory["sessions"] == 1
    assert inventory["selected_for_indexing"][0]["path"].endswith("valid.jsonl")


def test_discovery_of_one_thousand_sessions_is_fast():
    root = discovery.codex_root()
    for index in range(1000):
        create_session(root / "sessions" / str(index // 100) / f"{index}.jsonl", index + 1)

    started = time.monotonic()
    inventory = discovery.scan()
    elapsed = time.monotonic() - started

    assert inventory["sessions"] == 1000
    assert len(inventory["selected_for_indexing"]) == 5
    assert elapsed < 2
