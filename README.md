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

### Focused checks

Use `--step STEP_ID` with `install codex`, `status`, or `doctor`:

```console
blctx install codex --step background_service --json
blctx status --step data_directory --json
blctx doctor --step data_directory --no-color
```

Installation runs prerequisites first and reports them in `dependencies` and on
individual checks. A failed prerequisite prevents the dependent installer from
running. Status and doctor only verify the selected step; they never install its
prerequisites or repair removed state. Full reports retain the onboarding order.

JSON `success` and the process exit code describe the requested checks;
`ready` requires the complete applicable checklist and is always false for a
selected run. Passed selected checks exit 0; failed or blocked checks exit 1;
unknown step IDs and history selections combined with `--no-history` exit 2.
`--no-history` is also available on status and doctor. Authorized skips carry
`skip_reason: "no_history"`; summary wording cannot authorize a skip.

Stable IDs, in display order: `data_directory`, `background_service`, `codex_mcp`,
`codex_skills`, `codex_hooks`, `embedding_model`, `session_discovery`,
`history_index`, `service_health`, `mcp_health`, `history_retrieval`.

Production steps remain unimplemented in this foundation ticket. The subprocess
acceptance test uses an explicitly test-only file-backed implementation to prove
selected success and red/green/disconnect/red/reinstall behavior. It does not
claim that the product installer or uninstaller is implemented.
