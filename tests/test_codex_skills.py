import json
import os
import subprocess
from pathlib import Path


def run_blctx(home, *args):
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    return subprocess.run([str(Path(__file__).parents[1] / ".venv/bin/blctx"), *args, "--json"],
                          env=env, text=True, capture_output=True)


def test_codex_skill_red_green_uninstall_red_reinstall(tmp_path):
    sentinel = tmp_path / "transcript.jsonl"
    sentinel.write_text("keep")
    assert run_blctx(tmp_path, "status", "--step", "codex_skills").returncode == 1
    installed = run_blctx(tmp_path, "install", "codex", "--step", "codex_skills")
    assert installed.returncode == 0
    data = json.loads(installed.stdout)
    assert data["checks"][-1]["status"] == "passed"
    skill = tmp_path / ".codex/skills/base-layer-context/SKILL.md"
    assert skill.is_file() and "recent_context" in skill.read_text()
    assert run_blctx(tmp_path, "doctor", "--step", "codex_skills").returncode == 0
    assert run_blctx(tmp_path, "uninstall", "codex").returncode == 0
    assert not skill.exists() and sentinel.read_text() == "keep"
    assert run_blctx(tmp_path, "status", "--step", "codex_skills").returncode == 1
    assert run_blctx(tmp_path, "install", "codex", "--step", "codex_skills").returncode == 0


def test_existing_skill_collision_is_preserved(tmp_path):
    skill = tmp_path / ".codex/skills/base-layer-context/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("user-owned")
    result = run_blctx(tmp_path, "install", "codex", "--step", "codex_skills")
    assert result.returncode == 1
    assert skill.read_text() == "user-owned"
