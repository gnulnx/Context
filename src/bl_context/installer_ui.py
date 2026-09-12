"""Responsive terminal screens for ordered installer feedback."""

import os
import sys
import termios
import time
import tty

from rich.align import Align
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import ProgressBar, Spinner
from rich.table import Table
from rich.text import Text

from .checks import CheckStatus

MAX_PAGE_WIDTH = 112
SCREEN_ROW_MARGIN = 2
COMPLETION_WIDTH = 78


def render_install_completion(
    console, results, *, success, ready, selected_step=None, no_history=False,
    styled=True, interrupted=False,
):
    """Render one persistent ending, without alternate-screen height or cropping."""
    if interrupted:
        heading = "✗ Installation interrupted"
    elif selected_step:
        heading = "✓ Selected step complete" if success else "✗ Selected step failed"
    elif ready:
        heading = "✓ Installation complete"
    else:
        heading = "✗ Installation incomplete"
    color = "green" if success and not interrupted else "red"
    body = Text()
    body.append(heading, style=f"bold {color}")
    if selected_step:
        body.append("\nFull readiness not evaluated.")
    elif ready and not interrupted:
        body.append("\nContext is ready for Codex.")
    else:
        body.append("\nNot ready. Review the checks below.")

    blocked = 0
    for result in results:
        if result.status == CheckStatus.FAILED and result.summary == "Prerequisites unavailable":
            blocked += 1
            continue
        if result.status in (CheckStatus.FAILED, CheckStatus.WARNING) or (
            selected_step and result.step_id == selected_step
        ):
            body.append(f"\n\n{result.label}\n", style="bold")
            body.append(result.summary)
            for detail in (result.diagnostic, result.remediation):
                if detail:
                    body.append(f"\n{detail}")
    if blocked:
        body.append(f"\n\n{blocked} checks blocked by failed prerequisites.", style="dim")

    if ready and not interrupted:
        hooks = next((r for r in results if r.step_id == "codex_hooks"), None)
        hooks_installed = bool(
            hooks and hooks.status == CheckStatus.PASSED and not hooks.skip_reason
        )
        if not hooks_installed:
            body.append("\nAutomatic capture is disabled; hooks were not installed.", style="yellow")
        if no_history:
            body.append("\nExisting history was skipped (--no-history).", style="yellow")
        body.append("\n\nGet started\n", style="bold")
        actions = ["Open a new Codex session."]
        if hooks_installed:
            actions.append("Review and approve the Context hooks in /hooks.")
        actions.append(
            'Say: “Remember that this project uses pytest.”'
            if no_history
            else 'Ask: “What have we worked on over the last few days?”'
        )
        body.append("\n".join(f"  {number}. {action}" for number, action in enumerate(actions, 1)))

    body.append("\n\nUseful commands\n", style="bold")
    body.append("  blc status", style="cyan")
    body.append("     Check readiness\n")
    body.append("  blc doctor", style="cyan")
    body.append("     Diagnose problems")
    if ready and not interrupted:
        body.append(
            "\n\nConversation memory: 7 days\nSaved notes and tagged handoffs persist.",
            style="dim",
        )
    if not styled:
        return body
    return Align.center(
        Panel(
            body,
            title=" Base Layer Context ",
            title_align="left",
            border_style="bright_cyan" if success and not interrupted else "red",
            padding=(1, 2),
            width=max(8, min(COMPLETION_WIDTH, console.width - 4)),
        ),
        pad=False,
    )


def read_navigation_key():
    """Read one key without disabling terminal output processing."""
    terminal = None
    if sys.stdin.isatty():
        file_descriptor = sys.stdin.fileno()
    else:
        terminal = open("/dev/tty")
        file_descriptor = terminal.fileno()
    previous = termios.tcgetattr(file_descriptor)
    try:
        tty.setcbreak(file_descriptor)
        key = os.read(file_descriptor, 32).decode(
            sys.stdin.encoding or "utf-8", "replace"
        )
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, previous)
        if terminal is not None:
            terminal.close()
    if key == "\x03":
        raise KeyboardInterrupt
    if key == "\x04":
        raise EOFError
    return key


class InstallerForm:
    """Keep the complete installer visible while individual rows resolve."""

    def __init__(
        self,
        console,
        steps,
        *,
        show_diagnostics=False,
        full_screen=False,
        operation="Installing Codex integration",
    ):
        self.console = console
        self.steps = tuple(steps)
        self.show_diagnostics = show_diagnostics
        self.full_screen = full_screen
        self.operation = operation
        self.results = {}
        self.active_step = None
        self.embedding_progress = None
        self.history_index_progress = None
        self.progress_started = {}
        self.step_spinner = Spinner("dots", style="cyan")
        self.current_page = "installer"
        self.hooks_selected = 0
        self.live = Live(
            console=console,
            get_renderable=self.render,
            refresh_per_second=4,
            screen=full_screen,
            transient=False,
            vertical_overflow="crop",
        )

    def __enter__(self):
        self.live.start(refresh=True)
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.live.refresh()
        self.live.stop()
        if self.full_screen and exc_type is not None:
            # Normal completion is rendered by the CLI after readiness is known.
            # Exceptions bypass that path, but must still leave evidence behind.
            self.console.print(render_install_completion(
                self.console, self.results.values(), success=False, ready=False,
                interrupted=True,
            ))

    def start_step(self, step):
        self.active_step = step.step_id
        self.live.refresh()

    def finish_step(self, result):
        self.results[result.step_id] = result
        if self.active_step == result.step_id:
            self.active_step = None
        self.live.refresh()

    def update_embedding(self, stage, completed, total):
        self.progress_started.setdefault("embedding_model", time.monotonic())
        self.embedding_progress = (stage, completed, total)
        self.live.refresh()

    def update_history_index(self, stage, completed, total):
        self.progress_started.setdefault("history_index", time.monotonic())
        self.history_index_progress = (stage, completed, total)
        self.live.refresh()

    def choose_hooks(self):
        self.current_page = "hooks"
        self.hooks_selected = 0
        self.live.refresh()
        try:
            while True:
                key = read_navigation_key()
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
                    self.hooks_selected = 0
                    key = key[len(left_key) :]
                elif right_key:
                    self.hooks_selected = 1
                    key = key[len(right_key) :]
                if key in ("\r", "\n") and self.hooks_page_fits(
                    self.hooks_selected
                ):
                    return self.hooks_selected == 0
                self.live.refresh()
        finally:
            self.current_page = "installer"
            self.live.refresh()

    def page_width(self):
        return max(8, min(MAX_PAGE_WIDTH, self.console.width - 4))

    def page_fits(self, renderable):
        options = self.console.options.update(width=self.console.width, height=None)
        lines = self.console.render_lines(renderable, options, pad=False)
        return len(lines) <= self.usable_height()

    def usable_height(self):
        return max(self.console.height - SCREEN_ROW_MARGIN, 1)

    def center_page(self, panel):
        if not self.page_fits(panel):
            return self.render_resize_notice()
        return self.align_page(panel)

    def align_page(self, panel, *, full_screen=None):
        if full_screen is None:
            full_screen = self.full_screen
        return Align.center(
            panel,
            vertical="middle" if full_screen else None,
            pad=False,
            height=self.usable_height() if full_screen else None,
        )

    def render_resize_notice(self):
        width = max(8, min(62, self.console.width - 2))
        notice = Group(
            Text("Terminal resized", style="bold yellow"),
            Text(""),
            Text(
                "Enlarge this window to view the complete installer. "
                "The screen will restore automatically."
            ),
            Text(""),
            Text(
                f"Current size: {self.console.width} columns × "
                f"{self.console.height} rows",
                style="dim",
            ),
        )
        panel = Panel(
            notice,
            title=" Base Layer Context ",
            title_align="left",
            border_style="yellow",
            padding=(1, 2),
            width=width,
        )
        return self.align_page(panel)

    def hooks_panel(self, selected=0, *, compact=False):
        spacing = "\n" if compact else "\n\n"
        body = Text.from_markup(
            "Hooks keep new Codex work available without requiring manual saves."
            f"{spacing}[bold]What is installed[/bold]\n"
            "  One local handler for session start, turn complete, and session end."
            f"{spacing}[bold yellow]Security[/bold yellow]\n"
            "  • Hooks can run outside the Codex sandbox.\n"
            "  • Context can read the current transcript and working directory.\n"
            "  • Information is indexed into private local storage.\n"
            "  • Codex separately asks you to review and trust the handler."
        )
        enable = Text(
            " ▶ Enable automatic capture "
            if selected == 0
            else "   Enable automatic capture ",
            style="bold white on green" if selected == 0 else "dim",
        )
        skip = Text(
            " ▶ Skip for now " if selected == 1 else "   Skip for now ",
            style="bold black on yellow" if selected == 1 else "dim",
        )
        choices = Table.grid(expand=True)
        choices.add_column(ratio=1)
        choices.add_column(ratio=1)
        choices.add_row(Align.center(enable), Align.center(skip))
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
        disclosure = (
            body
            if compact
            else Panel(body, border_style="cyan", padding=(0, 2))
        )
        content = Group(
            Text("Automatic Codex capture", style="bold"),
            Text("Review optional lifecycle hooks", style="dim"),
            Text(""),
            disclosure,
            Text("Choose an option", style="bold"),
            choices,
            Text(""),
            Text.assemble(" ", feedback),
            Text(""),
            Text("←/→ Change selection    [Enter] Confirm", style="bold cyan"),
        )
        return Panel(
            content,
            title=" Base Layer Context ",
            title_align="left",
            border_style="bright_cyan",
            padding=(0, 2),
            width=self.page_width(),
        )

    def hooks_page_fits(self, selected=0):
        return self.page_fits(self.hooks_panel(selected)) or self.page_fits(
            self.hooks_panel(selected, compact=True)
        )

    def render_hooks_consent(self, selected=0):
        full = self.hooks_panel(selected)
        if self.page_fits(full):
            return self.align_page(full, full_screen=True)
        compact = self.hooks_panel(selected, compact=True)
        if self.page_fits(compact):
            return self.align_page(compact, full_screen=True)
        return self.render_resize_notice()

    def result_detail(self, result):
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
        return detail

    def step_table(self, *, show_details):
        table = Table.grid(padding=(0, 1), expand=True)
        table.add_column(width=2)
        if show_details:
            table.add_column(min_width=34)
            table.add_column(ratio=1)
        else:
            table.add_column(min_width=34, ratio=1)
        for step in self.steps:
            result = self.results.get(step.step_id)
            if result is not None:
                symbol, style = {
                    CheckStatus.PASSED: ("✓", "green"),
                    CheckStatus.FAILED: ("✗", "red"),
                    CheckStatus.WARNING: ("!", "yellow"),
                    CheckStatus.SKIPPED: ("–", "yellow"),
                }[result.status]
                row = [Text(symbol, style=style), Text(result.label, style=style)]
                if show_details:
                    row.append(self.result_detail(result))
                table.add_row(*row)
            elif self.active_step == step.step_id:
                detail = ""
                if step.step_id == "embedding_model" and self.embedding_progress:
                    detail = self.render_embedding_progress()
                elif step.step_id == "history_index" and self.history_index_progress:
                    detail = self.render_history_index_progress()
                row = [self.step_spinner, Text(step.label, style="cyan")]
                if show_details:
                    row.append(detail)
                table.add_row(*row)
            else:
                row = [Text("○", style="dim"), Text(step.label, style="dim")]
                if show_details:
                    row.append("")
                table.add_row(*row)
        return table

    def compact_detail(self):
        if self.active_step == "embedding_model" and self.embedding_progress:
            return self.render_embedding_progress()
        if self.active_step == "history_index" and self.history_index_progress:
            return self.render_history_index_progress()
        if self.active_step:
            step = next(item for item in self.steps if item.step_id == self.active_step)
            return Text(f"Working on: {step.label}", style="cyan")
        if self.results:
            result = self.results[next(reversed(self.results))]
            return Text.assemble((f"{result.label}: ", "bold"), result.summary)
        return Text("Preparing installation…", style="dim")

    def main_panel(self, *, show_details):
        resolved = len(self.results)
        resolution = Text(
            f"{resolved} of {len(self.steps)} steps resolved",
            style="dim",
        )
        if resolved == len(self.steps):
            failures = any(
                result.status == CheckStatus.FAILED
                for result in self.results.values()
            )
            resolution.append(" — ")
            skipped_or_warned = any(
                result.status in (CheckStatus.SKIPPED, CheckStatus.WARNING)
                or result.skip_reason
                for result in self.results.values()
            )
            resolution.append(
                "Failed checks require attention."
                if failures
                else "Completed with skips or warnings." if skipped_or_warned
                else "All checks passed.",
                style="bold red" if failures else "bold yellow" if skipped_or_warned else "bold green",
            )
        content = [
            Text(self.operation, style="bold"),
            Text("Persistent context for coding agents", style="dim"),
            Text(""),
            self.step_table(show_details=show_details),
        ]
        if not show_details:
            content.extend((Text(""), self.compact_detail()))
        content.extend(
            (
                Text(""),
                resolution,
            )
        )
        return Panel(
            Group(*content),
            title=" Base Layer Context ",
            title_align="left",
            border_style="bright_cyan",
            padding=(1 if show_details else 0, 2),
            width=self.page_width(),
        )

    def render_installer(self):
        detailed = self.main_panel(show_details=True)
        if self.page_fits(detailed):
            return self.align_page(detailed)
        compact = self.main_panel(show_details=False)
        return self.center_page(compact)

    def render(self):
        if self.current_page == "hooks":
            return self.render_hooks_consent(self.hooks_selected)
        return self.render_installer()

    def render_embedding_progress(self):
        stage, completed, total = self.embedding_progress
        details = Table.grid(padding=(0, 1))
        details.add_column()
        details.add_column(width=18)
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
            ProgressBar(total=total or 1, completed=completed, width=18),
            f"{completed / 1_000_000:.1f}/{total / 1_000_000:.1f} MB",
            eta,
        )
        return details

    def render_history_index_progress(self):
        stage, completed, total = self.history_index_progress
        details = Table.grid(padding=(0, 1))
        details.add_column()
        details.add_column(width=18)
        details.add_column()
        details.add_row(
            stage,
            ProgressBar(total=total or 1, completed=completed, width=18),
            f"{completed}/{total} sessions" if total else "0 sessions",
        )
        return details
