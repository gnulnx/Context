import time
from io import StringIO

import pytest
from rich.console import Console

from bl_context import checks
from bl_context.installer_ui import InstallerForm
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
    console = Console(file=output, width=160, force_terminal=False)
    form = InstallerForm(console, checks.STEPS)
    form.active_step = "embedding_model"
    form.progress_started["embedding_model"] = time.monotonic() - 1
    form.embedding_progress = ("Downloading embedding model", 50_000_000, 100_000_000)

    console.print(form.render())
    rendered = output.getvalue()

    assert all(step.label in rendered for step in checks.STEPS)
    model_row = next(line for line in rendered.splitlines() if "Embedding model" in line)
    assert "Downloading embedding model" in model_row
    assert "50.0/100.0 MB" in model_row


def test_live_form_keeps_history_progress_in_its_step_row():
    output = StringIO()
    console = Console(file=output, width=160, force_terminal=False)
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
    console = Console(file=output, width=160, force_terminal=False)
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
