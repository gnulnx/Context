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
and tested on Linux, along with the systemd user service; the remaining
integration checks report failure.
Full installation starts the Context user service. Models, Codex settings,
skills, hooks, and indexes are not installed yet.
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
configuration are preserved. Uninstall stops and disables the Context service before deactivating data
ownership. Codex integration removal will be added with those features.

To exercise the real lifecycle without changing your normal user installation:

```sh
python -m pytest -q tests/test_storage.py
```

The test runs the installed `blctx` entry point from `/` with an isolated HOME,
checks red → green → uninstall → red → reinstall → green, verifies idempotence
and preservation, and exercises purge. Full readiness stays false while the
remaining integrations are unimplemented.


### Background service (Linux systemd user session)

```sh
blctx install codex --step background_service
blctx status --step background_service --json
blctx doctor --step background_service
```

Installation creates an installation-specific `blctxd-UUID.service` in the
Context config directory and enables it through the systemd user manager. It
starts at user login; no sudo, system service, or lingering configuration is
required. An unavailable user manager/session bus fails with remediation.

The daemon uses the recorded absolute Python interpreter, reports readiness to
systemd only after binding a mode-0600 Unix socket in the private state directory,
and holds an exclusive writer lock on the data directory. Startup and shutdown
are bounded to 10 and 5 seconds; systemd restarts crashes. Logs are available with
`journalctl --user -u blctxd-UUID.service` (use the UUID in `installation.json`).

Verification checks active installation ownership, unit content, persistent
enablement, registration path, running PID/interpreter/arguments and matching
identity over private IPC. This proves daemon lifecycle, not indexing or retrieval;
those checks remain unimplemented. Default uninstall stops the process, disables
registration, and removes its unit/socket before deactivating ownership.

Ordinary tests isolate Context paths and deliberately use an unavailable service
bus. To run the real systemd acceptance test in a Linux user session:

```sh
BLCTX_SYSTEMD_TEST=1 python -m pytest -q tests/test_service.py
```

This opt-in test uses isolated Context directories and a unique unit on the
current user's real manager. It tests crash recovery, repeated installation,
missing-unit cleanup, and uninstall, and removes its registration afterward.
