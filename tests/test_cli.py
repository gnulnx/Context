import json
from io import StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from bl_context import checks
from bl_context.checks import CheckResult, CheckStatus, Step, run_checks
from bl_context.cli import choose_hooks_plain, main, rich_ui_enabled


def test_red_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    result = CliRunner().invoke(main, ["install", "codex", "--no-color"])
    assert result.exit_code == 1
    assert result.output == (Path(__file__).parent / "snapshots/install.txt").read_text()
    assert "\x1b" not in result.output


def test_rich_ui_requires_a_colored_interactive_terminal():
    interactive = Console(force_terminal=True, no_color=False)
    monochrome = Console(force_terminal=True, no_color=True)
    redirected = Console(force_terminal=False, no_color=False)

    assert rich_ui_enabled(interactive, no_color=False)
    assert not rich_ui_enabled(interactive, no_color=True)
    assert not rich_ui_enabled(monochrome, no_color=False)
    assert not rich_ui_enabled(redirected, no_color=False)


def test_plain_hooks_prompt_preserves_security_choice(monkeypatch):
    output = StringIO()
    console = Console(file=output, force_terminal=False, no_color=True)
    monkeypatch.setattr("bl_context.cli.click.confirm", lambda *args, **kwargs: False)

    assert choose_hooks_plain(console) is False
    rendered = output.getvalue()
    assert "outside the Codex sandbox" in rendered
    assert "manual saves still work" in rendered


def test_successful_install_exits_zero_and_invites_immediate_recall(monkeypatch):
    class ReadyAdapter:
        def install(self, **_observers):
            return [
                CheckResult(
                    step.step_id,
                    step.label,
                    CheckStatus.PASSED,
                    (
                        "Approve hooks on next Codex launch."
                        if step.step_id == "codex_hooks"
                        else "Ready"
                    ),
                )
                for step in checks.STEPS
            ]

    monkeypatch.setattr("bl_context.cli.installer_for", lambda _provider: ReadyAdapter())

    result = CliRunner().invoke(main, ["install", "codex", "--no-color"])

    assert result.exit_code == 0
    assert "✓ Codex Hooks" in result.output
    assert "Approve hooks on next Codex launch." in result.output
    assert (
        "Ready. Launch Codex and ask it to summarize your recent work."
        in result.output
    )
    assert 'Remember that the magic word is SomeMagicWord' in result.output
    assert 'Refresh context from tag test-handoff' in result.output


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
        assert all(c["summary"] in ("Prerequisites unavailable", "Data installation unavailable", "Background service unavailable", "Codex MCP unavailable", "Codex skill unavailable", "Codex hooks unavailable", "Embedding model unavailable", "Codex session discovery unavailable", "Recent Codex history is not indexed", "Service health unavailable", "MCP connection unavailable", "Historical memory retrieval unavailable", "Installation not finalized")
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
    assert "Run blc install codex --step data_directory" in result.output


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
