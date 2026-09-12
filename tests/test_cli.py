import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner
from click.utils import strip_ansi
from rich.console import Console

from bl_context import checks
from bl_context.checks import CheckResult, CheckStatus, Step, run_checks
from bl_context.cli import choose_hooks_plain, main, rich_ui_enabled


def test_red_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    def unavailable():
        raise RuntimeError("Background service unavailable in this test.")
    monkeypatch.setattr("bl_context.service.install", unavailable)
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


@pytest.fixture
def completed_install(monkeypatch):
    results = [
        CheckResult(step.step_id, step.label, CheckStatus.PASSED, "Verified.")
        for step in checks.STEPS
    ]

    class ReadyAdapter:
        def display_steps(self, command, selected_step):
            return checks.STEPS

        def install(self, **_observers):
            for step, result in zip(checks.STEPS, results):
                if "on_step_start" in _observers:
                    _observers["on_step_start"](step)
                    _observers["on_result"](result)
            return results

    monkeypatch.setattr("bl_context.cli.installer_for", lambda _provider: ReadyAdapter())
    return results


@pytest.mark.parametrize("flags", [[], ["--no-color"]])
def test_successful_install_exits_zero_and_invites_immediate_recall(completed_install, flags):
    result = CliRunner().invoke(main, ["install", "codex", *flags])

    assert result.exit_code == 0
    assert result.output.count("✓ Installation complete") == 1
    assert "Context is ready for Codex." in result.output
    assert "Review and approve the Context hooks in /hooks." in result.output
    assert "What have we worked on over the last few days?" in result.output
    assert "Saved notes and tagged handoffs persist." in result.output
    assert "╭" not in result.output
    assert "\x1b" not in result.output


def test_install_completion_remains_after_alternate_screen_closes(monkeypatch, completed_install):
    monkeypatch.setattr(
        "bl_context.cli.Console",
        lambda **kwargs: Console(force_terminal=True, width=80, height=24, **kwargs),
    )
    result = CliRunner().invoke(main, ["install", "codex"], color=True)

    assert result.exit_code == 0
    normal_buffer = strip_ansi(result.output.rsplit("\x1b[?1049l", 1)[1])
    assert normal_buffer.count("Installation complete") == 1
    assert "Installing Codex integration" not in normal_buffer
    assert "Try it in Codex" not in normal_buffer
    assert "╭" in normal_buffer.splitlines()[0]
    assert "╰" in normal_buffer.splitlines()[-1]
    assert len(normal_buffer.splitlines()) <= 21  # Leave room for the command and prompt.


def test_install_skipped_hooks_and_history_change_next_steps(completed_install):
    for index, result in enumerate(completed_install):
        if result.step_id == "codex_hooks":
            completed_install[index] = replace(result, summary="Hooks not installed.", skip_reason="optional")
        elif checks.STEPS[index].history:
            completed_install[index] = replace(result, status=CheckStatus.SKIPPED, skip_reason="no_history")
    result = CliRunner().invoke(main, ["install", "codex", "--no-history", "--no-color"])

    assert result.exit_code == 0
    assert "Automatic capture is disabled" in result.output
    assert "Existing history was skipped" in result.output
    assert "Remember that this project uses pytest." in result.output
    assert "/hooks" not in result.output
    assert "What have we worked on" not in result.output


def test_selected_install_does_not_claim_full_readiness(completed_install):
    result = CliRunner().invoke(main, ["install", "codex", "--step", "codex_hooks"])
    assert result.exit_code == 0
    assert "Selected step complete" in result.output
    assert "Full readiness not evaluated." in result.output
    assert "Context is ready" not in result.output
    assert "Get started" not in result.output


def test_install_json_stays_machine_readable(completed_install):
    result = CliRunner().invoke(main, ["install", "codex", "--json"])
    data = json.loads(result.output)
    assert result.exit_code == 0
    assert data["ready"] is True
    assert data["success"] is True
    assert all(item["step_id"] != "finalizing" for item in data["checks"])


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
        assert all(c["summary"] in ("Prerequisites unavailable", "Data installation unavailable", "Background service unavailable", "Codex MCP unavailable", "Codex skill unavailable", "Codex hooks unavailable", "Embedding model unavailable", "Codex session discovery unavailable", "Recent Codex history is not indexed", "Service health unavailable", "MCP connection unavailable", "Historical memory retrieval unavailable")
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
    assert sum(c["status"] == "failed" for c in checks) == 6


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
