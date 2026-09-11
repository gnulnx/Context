import os
import pty
import termios
import threading
import time
import tty
from io import StringIO

import pytest
from rich.console import Console

from bl_context import checks, storage
from bl_context.installer_ui import InstallerForm, read_navigation_key
from bl_context.installers import (
    CODEX_PROVIDER,
    CodexInstallerAdapter,
    InstallerAdapter,
    installer_for,
)


def test_codex_provider_resolves_through_adapter():
    adapter = installer_for(CODEX_PROVIDER)

    assert isinstance(adapter, InstallerAdapter)
    assert isinstance(adapter, CodexInstallerAdapter)
    assert adapter.provider == "openai/codex"


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="Unsupported provider"):
        installer_for("google/gemini")


def test_only_codex_install_calls_codex_install(monkeypatch):
    adapter = CodexInstallerAdapter()
    calls = []

    def record_codex_install(*, no_history=False, selected_step=None):
        calls.append((no_history, selected_step))
        return []

    monkeypatch.setattr(adapter, "codex_install", record_codex_install)
    monkeypatch.setattr(checks, "run_checks", lambda *args, **kwargs: [])

    adapter.verify()
    adapter.uninstall()
    assert calls == []

    adapter.install(no_history=True, selected_step="data_directory")
    assert calls == [(True, "data_directory")]


def test_live_form_contains_all_steps_and_inline_embedding_progress():
    output = StringIO()
    console = Console(file=output, width=160, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    form.active_step = "embedding_model"
    form.progress_started["embedding_model"] = time.monotonic() - 1
    form.embedding_progress = ("Downloading embedding model", 50_000_000, 100_000_000)

    console.print(form.render())
    rendered = output.getvalue()

    assert all(step.label in rendered for step in checks.STEPS)
    model_row = next(line for line in rendered.splitlines() if "Embedding model" in line)
    assert "Downloading embedding" in model_row
    assert "50.0/100.0 MB" in model_row


def test_active_step_spinner_advances_across_live_refreshes():
    output = StringIO()
    console = Console(file=output, width=160, height=40, force_terminal=False)
    clock = [0.0]
    console.get_time = lambda: clock[0]
    form = InstallerForm(console, checks.STEPS)
    form.active_step = "background_service"

    console.print(form.render())
    first_row = next(
        line for line in output.getvalue().splitlines() if "Background service" in line
    )
    output.seek(0)
    output.truncate(0)
    clock[0] = 0.2
    console.print(form.render())
    second_row = next(
        line for line in output.getvalue().splitlines() if "Background service" in line
    )

    assert first_row != second_row


def test_live_form_keeps_history_progress_in_its_step_row():
    output = StringIO()
    console = Console(file=output, width=160, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    form.active_step = "history_index"
    form.history_index_progress = ("Indexing recent sessions", 3, 5)

    console.print(form.render())
    rendered = output.getvalue()

    assert all(step.label in rendered for step in checks.STEPS)
    history_row = next(
        line for line in rendered.splitlines() if "Historical sessions indexed" in line
    )
    assert "Indexing recent sessions" in history_row
    assert "3/5 sessions" in history_row


def test_live_form_shows_history_index_failure_details():
    output = StringIO()
    console = Console(file=output, width=160, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    form.results["history_index"] = checks.CheckResult(
        "history_index",
        "Historical sessions indexed",
        checks.CheckStatus.FAILED,
        "Recent Codex history is not indexed",
        diagnostic="Indexer connection timed out",
        remediation="Retry the history index step.",
    )

    console.print(form.render())

    rendered = output.getvalue()
    assert "Indexer connection timed out" in rendered
    assert "Retry the history index step." in rendered


def test_hooks_consent_screen_explains_security_and_skipping():
    output = StringIO()
    console = Console(file=output, width=100, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)

    console.print(form.render_hooks_consent())

    rendered = output.getvalue()
    assert "One local handler for session start, turn complete, and session end" in rendered
    assert "outside the Codex sandbox" in rendered
    assert "Enable automatic capture" in rendered
    assert "Skip for now" in rendered
    assert "←/→ Change selection" in rendered
    assert "[Enter] Confirm" in rendered
    assert "Selected: Enable automatic capture" in rendered
    assert "Show technical details" not in rendered


def test_hooks_consent_screen_explains_selected_skip_consequence():
    output = StringIO()
    console = Console(file=output, width=100, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)

    console.print(form.render_hooks_consent(selected=1))

    rendered = output.getvalue()
    normalized = " ".join(rendered.split())
    assert "Selected: Skip for now" in rendered
    assert "No hooks or automatic capture" in rendered
    assert "manual" in normalized
    assert "saves still work" in normalized


def test_hooks_screen_has_a_complete_standard_terminal_layout():
    output = StringIO()
    console = Console(file=output, width=80, height=24, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)

    console.print(form.render_hooks_consent())

    rendered = output.getvalue()
    assert "Terminal resized" not in rendered
    assert "Security" in rendered
    assert "Enable automatic capture" in rendered
    assert "[Enter] Confirm" in rendered


def test_wide_short_hooks_screen_uses_complete_compact_layout():
    output = StringIO()
    console = Console(file=output, width=180, height=24, force_terminal=False)
    form = InstallerForm(console, checks.STEPS, full_screen=True)

    console.print(form.render_hooks_consent())

    rendered = output.getvalue()
    assert "Automatic Codex capture" in rendered
    assert "Hooks keep new Codex work" in rendered
    assert "[Enter] Confirm" in rendered
    assert len(rendered.splitlines()) <= 22


def test_undersized_terminal_gets_resize_notice_instead_of_clipping():
    output = StringIO()
    console = Console(file=output, width=60, height=15, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)

    console.print(form.render_hooks_consent())

    rendered = output.getvalue()
    assert "Terminal resized" in rendered
    assert "60 columns × 15 rows" in rendered
    assert "restore automatically" in " ".join(rendered.split())


def test_installer_window_is_bounded_and_centered_on_wide_terminals():
    output = StringIO()
    console = Console(file=output, width=140, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)

    console.print(form.render())

    border = next(line for line in output.getvalue().splitlines() if "╭" in line)
    assert border.index("╭") > 0
    assert len(border.rstrip()) <= 126


def test_full_screen_installer_is_centered_vertically():
    output = StringIO()
    console = Console(file=output, width=120, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS, full_screen=True)

    console.print(form.render())

    lines = output.getvalue().splitlines()
    border_index = next(index for index, line in enumerate(lines) if "╭" in line)
    assert border_index > 0


def test_complete_installer_uses_compact_layout_at_80_by_24():
    output = StringIO()
    console = Console(file=output, width=80, height=24, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    for step in checks.STEPS:
        form.results[step.step_id] = checks.CheckResult(
            step.step_id,
            step.label,
            checks.CheckStatus.PASSED,
            "A deliberately long verification result that cannot fit inline.",
        )

    console.print(form.render())

    rendered = output.getvalue()
    assert "Terminal resized" not in rendered
    assert all(step.label in rendered for step in checks.STEPS)
    assert len(rendered.splitlines()) <= 24


def test_completed_installer_makes_failure_state_explicit():
    output = StringIO()
    console = Console(file=output, width=160, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    for step in checks.STEPS:
        status = (
            checks.CheckStatus.FAILED
            if step.step_id == "history_index"
            else checks.CheckStatus.PASSED
        )
        form.results[step.step_id] = checks.CheckResult(
            step.step_id, step.label, status, "Result"
        )

    console.print(form.render())

    assert "Failed checks require attention." in output.getvalue()


def test_full_screen_installer_reprints_final_state_after_live_screen_closes(
    monkeypatch,
):
    output = StringIO()
    console = Console(file=output, width=160, height=40, force_terminal=False)
    form = InstallerForm(console, checks.STEPS, full_screen=True)
    form.results["history_index"] = checks.CheckResult(
        "history_index",
        "Historical sessions indexed",
        checks.CheckStatus.FAILED,
        "Recent Codex history is not indexed",
    )
    events = []
    monkeypatch.setattr(form.live, "refresh", lambda: events.append("refresh"))
    monkeypatch.setattr(form.live, "stop", lambda: events.append("stop"))

    form.__exit__(None, None, None)

    assert events == ["refresh", "stop"]
    assert "Historical sessions indexed" in output.getvalue()
    assert "Recent Codex history is not indexed" in output.getvalue()


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("\n",), True),
        (("\x1b[C", "\n"), False),
        (("\x1b[C\n",), False),
        (("\x1b[C", "\x1b[D", "\n"), True),
    ],
)
def test_hooks_choice_uses_arrow_keys_and_enter(monkeypatch, keys, expected):
    console = Console(file=StringIO(), width=100, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    keypresses = iter(keys)
    monkeypatch.setattr(
        "bl_context.installer_ui.read_navigation_key", keypresses.__next__
    )
    pages = []
    monkeypatch.setattr(form.live, "refresh", lambda: pages.append(form.current_page))

    assert form.choose_hooks() is expected
    assert "hooks" in pages
    assert pages[-1] == "installer"


def test_navigation_key_reader_preserves_terminal_output_processing(monkeypatch):
    master, slave = pty.openpty()
    terminal = os.fdopen(slave, "r", encoding="utf-8", closefd=True)
    previous = termios.tcgetattr(terminal.fileno())
    observed = []
    original_setcbreak = tty.setcbreak
    cbreak_ready = threading.Event()

    def record_cbreak(file_descriptor):
        original_setcbreak(file_descriptor)
        observed.append(termios.tcgetattr(file_descriptor))
        cbreak_ready.set()

    def write_arrow_key():
        cbreak_ready.wait()
        os.write(master, b"\x1b[C")

    monkeypatch.setattr("bl_context.installer_ui.sys.stdin", terminal)
    monkeypatch.setattr("bl_context.installer_ui.tty.setcbreak", record_cbreak)
    writer = threading.Thread(target=write_arrow_key)
    writer.start()
    try:
        assert read_navigation_key() == "\x1b[C"
        restored = termios.tcgetattr(terminal.fileno())
    finally:
        writer.join()
        os.close(master)
        terminal.close()

    assert observed[0][tty.OFLAG] & termios.OPOST
    assert restored == previous


def test_codex_install_uses_hook_choice(monkeypatch):
    adapter = CodexInstallerAdapter()
    seen = []

    monkeypatch.setattr(checks, "run_checks", lambda *args, **kwargs: kwargs)
    monkeypatch.setattr("bl_context.installers.codex_hooks.registered", lambda: False)

    arguments = adapter.install(choose_hooks=lambda: seen.append("asked") or False)

    hook = next(step for step in checks.STEPS if step.step_id == "codex_hooks")
    assert arguments["should_install"](hook) is False
    assert seen == ["asked"]


def test_skipped_hooks_are_a_green_confirmation(monkeypatch):
    monkeypatch.setattr("bl_context.checks.codex_hooks.skipped", lambda: True)
    step = next(step for step in checks.STEPS if step.step_id == "codex_hooks")

    result = step.verify()

    assert result.status == checks.CheckStatus.PASSED
    assert result.label == "Codex Hooks"
    assert result.summary == "Hooks not installed."


def test_enabled_hooks_are_a_green_confirmation(monkeypatch):
    monkeypatch.setattr("bl_context.checks.codex_hooks.skipped", lambda: False)
    monkeypatch.setattr(
        "bl_context.checks.codex_hooks.verify_registration", lambda: None
    )
    step = next(step for step in checks.STEPS if step.step_id == "codex_hooks")

    result = step.verify()

    assert result.status == checks.CheckStatus.PASSED
    assert result.label == "Codex Hooks"
    assert result.summary == "Approve hooks on next Codex launch."


def test_live_form_renders_skipped_hooks_as_green():
    output = StringIO()
    console = Console(file=output, width=160, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    form.results["codex_hooks"] = checks.CheckResult(
        "codex_hooks",
        "Codex Hooks",
        checks.CheckStatus.PASSED,
        "Hooks not installed.",
        skip_reason="optional",
    )

    console.print(form.render())

    rendered = output.getvalue()
    assert "✓  Codex Hooks" in rendered
    assert "Hooks not installed." in rendered


def test_finalizing_waits_and_records_completion(monkeypatch):
    storage.install()
    waits = []
    monkeypatch.setattr(checks, "sleep", waits.append)
    step = next(step for step in checks.STEPS if step.step_id == "finalizing")

    step.install()

    assert waits == [2.5]
    assert step.verify().status == checks.CheckStatus.PASSED
