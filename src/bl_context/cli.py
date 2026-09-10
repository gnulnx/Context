"""Click commands and Rich rendering for the intentionally red CLI."""

import json

import click
from rich.console import Console

from .checks import CheckStatus, UNINSTALL_STEPS, is_ready, run_checks


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


def report(command, json_output, no_color, *, no_history=False):
    ctx = click.get_current_context()
    json_output = json_output or ctx.obj["json_output"]
    no_color = no_color or ctx.obj["no_color"]
    results = run_checks(
        **({"steps": UNINSTALL_STEPS} if command == "uninstall" else {}),
        install=command == "install", no_history=no_history,
    )
    ready = is_ready(results)
    if json_output:
        click.echo(json.dumps({
            "schema_version": 1, "command": command,
            "ready": ready, "exit_code": 0 if ready else 1,
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
            if command == "doctor":
                console.print(f"   {result.diagnostic}\n   {result.remediation}\n", markup=False)
        console.print(" Ready." if ready else " Not ready.")
    ctx.exit(0 if ready else 1)


@main.command()
@click.argument("agent", type=click.Choice(["codex"]))
@click.option("--no-history", is_flag=True, help="Skip historical discovery, indexing, and verification.")
@output_options
def install(agent, no_history, json_output, no_color):
    """Install an agent integration (currently unimplemented)."""
    report("install", json_output, no_color, no_history=no_history)


@main.command()
@output_options
def status(json_output, no_color):
    """Check readiness without changing state."""
    report("status", json_output, no_color)


@main.command()
@output_options
def doctor(json_output, no_color):
    """Explain failed checks (currently all unimplemented)."""
    report("doctor", json_output, no_color)


@main.command()
@click.argument("agent", type=click.Choice(["codex"]))
@output_options
def uninstall(agent, json_output, no_color):
    """Uninstall shell; currently removes nothing and reports failure."""
    report("uninstall", json_output, no_color)
