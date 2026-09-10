# Base Layer Context

Persistent context for coding agents.

This repository contains the Python package, onboarding CLI, and the first
local installation step. Historical recall and agent integrations are still
under development.

## Install

```console
pip install bl-context
```

```python
import bl_context

print(bl_context.__version__)
```

## CLI development preview

The onboarding CLI uses Click and Rich. The data directory step is implemented
and tested on Linux; the remaining integration checks report failure.
No services, models, Codex settings, skills, hooks, or indexes are installed.
Use the checkout installation below to test this development version.

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

Full install, status, and doctor currently exit 1 because integrations remain
unimplemented. Selected data checks exit 0 after installation. Uninstall exits 0
when installation ownership is deactivated.
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

The data step has a real subprocess acceptance test covering install, status,
doctor, uninstall, reinstall and purge. The execution foundation also retains
a test-only file-backed fixture for generic dependency behavior.


### Private local installation (Linux)

`blctx install codex --step data_directory` creates these user directories with
mode `0700`, respecting absolute `XDG_DATA_HOME`, `XDG_CONFIG_HOME`,
`XDG_CACHE_HOME`, and `XDG_STATE_HOME` overrides:

| Purpose | Default path |
| --- | --- |
| Data | `~/.local/share/bl-context` |
| Configuration | `~/.config/bl-context` |
| Cache | `~/.cache/bl-context` |
| State | `~/.local/state/bl-context` |

The data directory contains `context.db`, initialized atomically with schema
version 1 and an installation identifier. The state directory contains a
versioned `installation.json` recording active ownership, owned files and
created directories, and absolute interpreter/CLI paths for later service use.
Both files have mode `0600`. Reinstall retains the installation identifier and
existing data. An interrupted initial schema creation can be retried.

Status and doctor require private writable directories, a valid schema matching
the manifest, active ownership, and available recorded executables. They do not
repair permissions or recreate missing state. Existing unowned databases,
unsupported manifests, symbolic links, and unsafe permissions fail with diagnostics
instead of being overwritten or silently adopted.

`blctx uninstall codex` deactivates the manifest and retains data/cache. Retained
files alone do not pass verification. `blctx uninstall codex --purge` removes only
the two explicitly owned files and empty directories created by Context. Unknown
files, pre-existing directories, original Codex transcripts, and unrelated
configuration are preserved. Service and Codex integration removal will be added
with those features; this version installs neither.

To exercise the real lifecycle without changing your normal user installation:

```sh
python -m pytest -q tests/test_storage.py
```

The test runs the installed `blctx` entry point from `/` with an isolated HOME,
checks red → green → uninstall → red → reinstall → green, verifies idempotence
and preservation, and exercises purge. Full readiness stays false while the
remaining integrations are unimplemented.
