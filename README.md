# Base Layer Context

Persistent context for coding agents.

This repository currently contains the pre-development Python package that
reserves the `bl-context` name and its release infrastructure. Product
functionality has not been implemented yet.

## Install

```console
pip install bl-context
```

```python
import bl_context

print(bl_context.__version__)
```

## CLI development preview

The feature branch defines the onboarding experience using Click and Rich.
Every integration check is intentionally unimplemented and reports failure.
No services, models, Codex settings, skills, hooks, or indexes are installed.
The published 0.0.1 package does not contain this CLI yet.

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
blctx install codex --no-color
blctx install codex --no-history --json
blctx status
blctx doctor
blctx uninstall codex
python -m pytest -q
```

All four operational commands currently exit with code 1 and report `Not ready.`
Usage errors exit with code 2; help exits with code 0. `--json` emits only a
structured report, including ordered checks and the exit code. `--no-color`
disables colors. Both output flags work before the command or after its arguments.
`--no-history` skips the three history checks without claiming they passed;
the remaining failures still prevent readiness.

The tests pass when these intentional failures are reported accurately. The
installer snapshot is the visible contract for subsequent implementation.
