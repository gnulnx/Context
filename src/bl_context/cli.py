"""Click commands and Rich rendering for the Context CLI."""

import json
from contextlib import nullcontext

import click
from rich.console import Console

from .checks import CheckStatus, checks_succeeded, is_ready
from .embedding import progress
from .explore import explore
from .history_index import progress as history_progress
from .installer_ui import InstallerForm
from .installers import CODEX_PROVIDER, installer_for
from .retrieval_cli import context, index_command, index_status, recent, search

CLI_PROVIDERS = {"codex": CODEX_PROVIDER}


def rich_ui_enabled(console, no_color):
    return bool(
        console is not None
        and console.is_terminal
        and not no_color
        and not console.no_color
    )


def choose_hooks_plain(console):
    console.print("\n Optional Codex hooks", markup=False)
    console.print(
        " Hooks enable automatic local capture and run outside the Codex sandbox.",
        markup=False,
    )
    console.print(
        " Codex will separately ask you to review and trust the handler.",
        markup=False,
    )
    console.print(
        " Search, retrieval, and manual saves still work if you skip.\n",
        markup=False,
    )
    return click.confirm(" Install hooks for automatic capture?", default=True)


def output_options(function):
    function = click.option("--no-color", is_flag=True, help="Disable ANSI colors.")(function)
    return click.option("--json", "json_output", is_flag=True, help="Emit JSON only.")(function)


@click.group()
@output_options
@click.pass_context
def main(ctx, json_output, no_color):
    """Base Layer Context: persistent context for coding agents."""
    ctx.ensure_object(dict)
    ctx.obj.update(json_output=json_output, no_color=no_color)


def report(
    command,
    json_output,
    no_color,
    *,
    provider=CODEX_PROVIDER,
    no_history=False,
    step=None,
    purge=False,
):
    ctx = click.get_current_context()
    json_output = json_output or ctx.obj["json_output"]
    no_color = no_color or ctx.obj["no_color"]
    console = None if json_output else Console(no_color=no_color, highlight=False)
    live_form = None
    rich_ui = rich_ui_enabled(console, no_color)
    if console is not None and not rich_ui:
        console.print("\n Base Layer Context - Persistent memory for coding agents\n")
    try:
        adapter = installer_for(provider)
        if rich_ui:
            operations = {
                "install": "Installing Codex integration",
                "uninstall": "Uninstalling Codex integration",
                "status": "Checking Codex integration",
                "doctor": "Diagnosing Codex integration",
            }
            live_form = InstallerForm(
                console,
                adapter.display_steps(command, step),
                show_diagnostics=command == "doctor",
                full_screen=command == "install",
                operation=operations[command],
            )
            progress_context = progress(callback=live_form.update_embedding)
            history_progress_context = history_progress(
                callback=live_form.update_history_index
            )
            observers = {
                "on_step_start": live_form.start_step,
                "on_result": live_form.finish_step,
            }
            if command == "install":
                observers["choose_hooks"] = live_form.choose_hooks
        else:
            progress_console = (
                None if console is not None and console.is_terminal else console
            )
            progress_context = progress(progress_console)
            history_progress_context = history_progress()
            observers = (
                {"choose_hooks": lambda: choose_hooks_plain(console)}
                if command == "install"
                and console is not None
                and console.is_terminal
                else {}
            )
        with live_form or nullcontext(), progress_context, history_progress_context:
            if command == "install":
                results = adapter.install(
                    no_history=no_history, selected_step=step, **observers
                )
            elif command == "uninstall":
                results = adapter.uninstall(purge=purge, **observers)
            else:
                results = adapter.verify(
                    no_history=no_history, selected_step=step, **observers
                )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    success = checks_succeeded(results, no_history=no_history)
    ready = is_ready(results, no_history=no_history, selected_step=step)
    if json_output:
        click.echo(json.dumps({
            "schema_version": 1, "command": command,
            "ready": ready, "success": success, "selected_step": step,
            "dependencies": [r.step_id for r in results if r.dependency],
            "exit_code": 0 if success else 1,
            "checks": [r.to_dict() for r in results],
        }))
    elif live_form is None:
        symbols = {
            CheckStatus.PASSED: ("✓", "green"),
            CheckStatus.FAILED: ("✗", "red"),
            CheckStatus.WARNING: ("!", "yellow"),
            CheckStatus.SKIPPED: ("–", "yellow"),
        }
        for result in results:
            symbol, color = symbols[result.status]
            console.print(f" {symbol} {result.label}", style=color, markup=False)
            console.print(f"   {result.summary}\n", markup=False)
            if result.dependency:
                console.print("   Prerequisite for selected step.\n")
            if command == "doctor" or (
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
                console.print(f"   {result.diagnostic}\n   {result.remediation}\n", markup=False)
    if not json_output:
        if command == "uninstall" and success:
            console.print(" Uninstalled.")
        elif step:
            console.print(
                " Selected check passed. Full readiness not evaluated."
                if success
                else " Selected check failed. Not ready."
            )
        else:
            console.print(
                " Ready. Launch Codex and ask it to summarize your recent sessions."
                if ready
                else " Not ready."
            )
    ctx.exit(0 if success else 1)


@main.command()
@click.option("--step", help="Run one step by its stable ID, resolving install prerequisites.")
@click.argument("agent", type=click.Choice(["codex"]))
@click.option("--no-history", is_flag=True, help="Skip historical discovery, indexing, and verification.")
@output_options
def install(agent, no_history, json_output, no_color, step):
    """Install an agent integration, or one selected step."""
    report(
        "install",
        json_output,
        no_color,
        provider=CLI_PROVIDERS[agent],
        no_history=no_history,
        step=step,
    )


@main.command()
@click.option("--step", help="Verify one step without repairing state.")
@click.option("--no-history", is_flag=True, help="Skip explicit history checks.")
@output_options
def status(json_output, no_color, step, no_history):
    """Check readiness without changing state."""
    report("status", json_output, no_color, step=step, no_history=no_history)


@main.command()
@click.option("--step", help="Verify one step without repairing state.")
@click.option("--no-history", is_flag=True, help="Skip explicit history checks.")
@output_options
def doctor(json_output, no_color, step, no_history):
    """Explain readiness and failed checks."""
    report("doctor", json_output, no_color, step=step, no_history=no_history)


@main.command()
@click.argument("agent", type=click.Choice(["codex"]))
@output_options
@click.option("--purge", is_flag=True, help="Remove explicitly owned Context data; preserve unknown files.")
def uninstall(agent, json_output, no_color, purge):
    """Deactivate installation ownership, retaining data unless --purge is given."""
    report(
        "uninstall",
        json_output,
        no_color,
        provider=CLI_PROVIDERS[agent],
        purge=purge,
    )


main.add_command(explore)

for command in (index_command, index_status, recent, search, context):
    main.add_command(command)
