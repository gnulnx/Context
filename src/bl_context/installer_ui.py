"""Live terminal form for ordered installer feedback."""

import time

import click
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import ProgressBar, Spinner
from rich.table import Table
from rich.text import Text

from .checks import CheckStatus


class InstallerForm:
    """Keep the complete installer visible while individual rows resolve."""

    def __init__(self, console, steps, *, show_diagnostics=False):
        self.console = console
        self.steps = tuple(steps)
        self.show_diagnostics = show_diagnostics
        self.results = {}
        self.active_step = None
        self.embedding_progress = None
        self.history_index_progress = None
        self.progress_started = {}
        self.live = Live(
            self.render(),
            console=console,
            refresh_per_second=12,
            transient=False,
        )

    def __enter__(self):
        self.live.start(refresh=True)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.live.update(self.render(), refresh=True)
        self.live.stop()

    def start_step(self, step):
        self.active_step = step.step_id
        self.live.update(self.render(), refresh=True)

    def finish_step(self, result):
        self.results[result.step_id] = result
        if self.active_step == result.step_id:
            self.active_step = None
        self.live.update(self.render(), refresh=True)

    def update_embedding(self, stage, completed, total):
        self.progress_started.setdefault("embedding_model", time.monotonic())
        self.embedding_progress = (stage, completed, total)
        self.live.update(self.render(), refresh=True)

    def update_history_index(self, stage, completed, total):
        self.progress_started.setdefault("history_index", time.monotonic())
        self.history_index_progress = (stage, completed, total)
        self.live.update(self.render(), refresh=True)

    def choose_hooks(self):
        self.live.stop()
        try:
            with self.console.screen():
                selected = 0
                with Live(
                    self.render_hooks_consent(selected),
                    console=self.console,
                    auto_refresh=False,
                    transient=False,
                ) as selector:
                    while True:
                        key = click.getchar()
                        left_key = next(
                            (
                                value
                                for value in ("\x1b[D", "\x1bOD", "\xe0K")
                                if key.startswith(value)
                            ),
                            None,
                        )
                        right_key = next(
                            (
                                value
                                for value in ("\x1b[C", "\x1bOC", "\xe0M")
                                if key.startswith(value)
                            ),
                            None,
                        )
                        if left_key:
                            selected = 0
                            key = key[len(left_key) :]
                        elif right_key:
                            selected = 1
                            key = key[len(right_key) :]
                        if key in ("\r", "\n"):
                            return selected == 0
                        selector.update(
                            self.render_hooks_consent(selected), refresh=True
                        )
        finally:
            self.live.start(refresh=True)

    def render_hooks_consent(self, selected=0):
        body = Text.from_markup(
            "Hooks keep new Codex work available without requiring manual saves.\n\n"
            "[bold]What is installed[/bold]\n"
            "  One local handler for session start, turn complete, and session end.\n\n"
            "[bold yellow]Security[/bold yellow]\n"
            "  • Hooks can run outside the Codex sandbox.\n"
            "  • Context can read the current transcript and working directory.\n"
            "  • Information is indexed into private local storage.\n"
            "  • Codex separately asks you to review and trust the handler."
        )
        choices = Text(justify="center")
        choices.append(
            " ▶ Enable automatic capture "
            if selected == 0
            else "   Enable automatic capture ",
            style="bold white on green" if selected == 0 else "dim",
        )
        choices.append("      ")
        choices.append(
            " ▶ Skip for now " if selected == 1 else "   Skip for now ",
            style="bold black on yellow" if selected == 1 else "dim",
        )
        if selected == 0:
            feedback = Text.from_markup(
                "[bold green]Selected: Enable automatic capture.[/bold green] Installs "
                "the handler; Codex asks for separate trust on its next launch."
            )
        else:
            feedback = Text.from_markup(
                "[bold yellow]Selected: Skip for now.[/bold yellow] No hooks or automatic "
                "capture; search, retrieval, and manual saves still work."
            )
        return Group(
            Text("\n Base Layer Context - Automatic Codex capture\n", style="bold"),
            Panel(body, border_style="cyan", padding=(0, 2)),
            Text("\n Choose an option", style="bold"),
            choices,
            Text("\n"),
            Text.assemble(" ", feedback),
            Text("\n  ←/→ Change selection    [Enter] Confirm", style="bold cyan"),
        )

    def render(self):
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=2)
        table.add_column(min_width=34)
        table.add_column(ratio=1)
        for step in self.steps:
            result = self.results.get(step.step_id)
            if result is not None:
                symbol, style = {
                    CheckStatus.PASSED: ("✓", "green"),
                    CheckStatus.FAILED: ("✗", "red"),
                    CheckStatus.WARNING: ("!", "yellow"),
                    CheckStatus.SKIPPED: ("–", "yellow"),
                }[result.status]
                detail = result.summary
                if self.show_diagnostics or (
                    result.step_id
                    in (
                        "embedding_model",
                        "history_index",
                        "service_health",
                        "mcp_health",
                        "history_retrieval",
                    )
                    and result.status == CheckStatus.FAILED
                    and result.summary != "Prerequisites unavailable"
                ):
                    detail = "\n".join(
                        value
                        for value in (detail, result.diagnostic, result.remediation)
                        if value
                    )
                table.add_row(
                    Text(symbol, style=style),
                    Text(result.label, style=style),
                    detail,
                )
            elif self.active_step == step.step_id:
                detail = ""
                if step.step_id == "embedding_model" and self.embedding_progress:
                    detail = self.render_embedding_progress()
                elif step.step_id == "history_index" and self.history_index_progress:
                    detail = self.render_history_index_progress()
                table.add_row(Spinner("dots", style="cyan"), Text(step.label, style="cyan"), detail)
            else:
                table.add_row(Text("○", style="dim"), Text(step.label, style="dim"), "")
        return Group(
            Text("\n Base Layer Context - Persistent memory for coding agents\n"),
            table,
        )

    def render_embedding_progress(self):
        stage, completed, total = self.embedding_progress
        details = Table.grid(padding=(0, 1))
        details.add_column()
        details.add_column(width=24)
        details.add_column()
        details.add_column()
        elapsed = max(
            time.monotonic() - self.progress_started["embedding_model"], 0.001
        )
        rate = completed / elapsed
        remaining = max(total - completed, 0)
        eta = f"{remaining / rate:.0f}s" if rate and remaining else "0s"
        details.add_row(
            stage,
            ProgressBar(total=total or 1, completed=completed, width=24),
            f"{completed / 1_000_000:.1f}/{total / 1_000_000:.1f} MB",
            eta,
        )
        return details

    def render_history_index_progress(self):
        stage, completed, total = self.history_index_progress
        details = Table.grid(padding=(0, 1))
        details.add_column()
        details.add_column(width=24)
        details.add_column()
        details.add_row(
            stage,
            ProgressBar(total=total or 1, completed=completed, width=24),
            f"{completed}/{total} sessions" if total else "0 sessions",
        )
        return details
