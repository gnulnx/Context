import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bl_context.cli import main
from bl_context.checks import CheckStatus, Step, run_checks


def test_red_snapshot(monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    result = CliRunner().invoke(main, ["install", "codex", "--no-color"])
    assert result.exit_code == 1
    assert result.output == (Path(__file__).parent / "snapshots/install.txt").read_text()
    assert "\x1b" not in result.output


@pytest.mark.parametrize("args", [
    ["status"], ["doctor"],
])
def test_json_failures_and_no_changes(args, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    sentinel = tmp_path / "user-config"
    sentinel.write_text("preserve me")
    for _ in range(2):
        result = CliRunner().invoke(main, [*args, "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["ready"] is False
        assert data["exit_code"] == 1
        assert all(c["status"] == "failed" for c in data["checks"])
        assert all(c["summary"] in ("Not implemented", "Prerequisites unavailable", "Data installation unavailable", "Background service unavailable")
                   for c in data["checks"])
        assert all(c["duration"] >= 0 for c in data["checks"])
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text() == "preserve me"


def test_no_history():
    result = CliRunner().invoke(main, ["--json", "install", "codex", "--no-history"])
    assert result.exit_code == 1
    checks = json.loads(result.output)["checks"]
    assert [c["step_id"] for c in checks if c["status"] == "skipped"] == [
        "session_discovery", "history_index", "history_retrieval",
    ]
    assert sum(c["status"] == "failed" for c in checks) == 7


def test_doctor_explains_failure():
    result = CliRunner().invoke(main, ["doctor", "--no-color"])
    assert result.exit_code == 1
    assert "rerunning cannot repair this yet" in result.output


def test_install_does_not_earn_pass():
    assert run_checks([Step("test", "Test")], install=True)[0].status == CheckStatus.FAILED


def test_exception_becomes_failure():
    class Broken(Step):
        def verify(self):
            raise RuntimeError("probe failed")
    result = run_checks([Broken("broken", "Broken"), Step("next", "Next")])
    assert len(result) == 2
    assert result[0].status == CheckStatus.FAILED
    assert result[0].diagnostic == "probe failed"


@pytest.mark.parametrize("args", [[], ["--help"], ["install", "--help"]])
def test_help(args):
    result = CliRunner().invoke(main, args)
    assert result.exit_code in (0, 2)
    assert "Usage:" in result.output


def test_unknown_agent_is_usage_error():
    assert CliRunner().invoke(main, ["install", "gemini"]).exit_code == 2
