"""Live terminal form for ordered installer feedback."""

import time

from rich.console import Group
from rich.live import Live
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
                table.add_row(Text(symbol, style=style), Text(step.label, style=style), detail)
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
            Text("\n Base Layer Context\n Persistent context for coding agents\n"),
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
