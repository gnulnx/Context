"""Click commands and Rich rendering for the intentionally red CLI."""

import json
from dataclasses import replace

import click
from rich.console import Console

from .checks import CheckStatus, UNINSTALL_STEPS, is_ready, run_checks, checks_succeeded


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


def report(command, json_output, no_color, *, no_history=False, step=None, purge=False):
    ctx = click.get_current_context()
    json_output = json_output or ctx.obj["json_output"]
    no_color = no_color or ctx.obj["no_color"]
    try:
        results = run_checks(
            **({"steps": tuple(replace(s, purge=True) for s in UNINSTALL_STEPS)
               if purge else UNINSTALL_STEPS} if command == "uninstall" else {}),
            install=command in ("install", "uninstall"), no_history=no_history, selected_step=step,
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
    else:
        console = Console(no_color=no_color, highlight=False)
        console.print("\n Base Layer Context\n Persistent context for coding agents\n")
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
            if command == "doctor":
                console.print(f"   {result.diagnostic}\n   {result.remediation}\n", markup=False)
        if command == "uninstall" and success:
            console.print(" Uninstalled.")
        elif step:
            console.print(" Selected check passed. Full readiness not evaluated."
                          if success else " Selected check failed. Not ready.")
        else:
            console.print(" Ready." if ready else " Not ready.")
    ctx.exit(0 if success else 1)


@main.command()
@click.option("--step", help="Run one step by its stable ID, resolving install prerequisites.")
@click.argument("agent", type=click.Choice(["codex"]))
@click.option("--no-history", is_flag=True, help="Skip historical discovery, indexing, and verification.")
@output_options
def install(agent, no_history, json_output, no_color, step):
    """Install an agent integration, or one selected step."""
    report("install", json_output, no_color, no_history=no_history, step=step)


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
    report("uninstall", json_output, no_color, purge=purge)
