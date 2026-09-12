# Base Layer Context — Developer & Contributor Guide

This guide covers local development, testing harnesses, step-by-step installation verification, and low-level CLI development flags for Base Layer Context (`blc`).

---

## 1. Development Environment Setup

```bash
# Clone repository and create virtualenv
git clone git@github.com:gnulnx/Context.git
cd Context
python3 -m venv .venv
source .venv/bin/activate

# Install editable package with dev dependencies
python -m pip install -e '.[dev]'

# Verify test baseline
python -m pytest -q
python -m ruff check src tests
```

---

## 2. The Onboarding Verification Pipeline

The onboarding CLI runs an ordered checklist of 11 distinct subsystem steps to ensure total system health and data isolation.

### Stable Step IDs (in execution order)
1. `data_directory`: Creates mode `0700` user directories and atomic schema v1 in SQLite.
2. `background_service`: Registers and starts `blctxd` through systemd on Linux or launchd on macOS.
3. `codex_mcp`: Registers stdio MCP server in `$CODEX_HOME/config.json`.
4. `codex_skills`: Installs `base-layer-context` skill into `$CODEX_HOME/skills`.
5. `embedding_model`: Downloads & cryptographically verifies `BAAI/bge-small-en` (384-dim CPU).
6. `session_discovery`: Inspects `$CODEX_HOME/sessions` and selects the newest 5 sessions.
7. `history_index`: Indexes selected sessions into local SQLite and Qdrant vector storage.
8. `service_health`: Verifies IPC communication and exclusive daemon database locks.
9. `mcp_health`: Spawns stdio MCP subprocess, verifies 8 tools, and tests protocol round-trip.
10. `history_retrieval`: Runs end-to-end semantic search and context expansion through MCP.
11. `codex_hooks`: Registers `SessionStart`, `Stop`, and `SessionEnd` hooks in `$CODEX_HOME/hooks.json`.

---

## 3. Focused Step Execution & Diagnostics

You can run individual steps for focused testing and development using `--step`:

```bash
# Test background service installation and verification
blc install codex --step background_service --json
blc status --step background_service --json
blc doctor --step background_service --no-color

# Test data directory creation and schema integrity
blc install codex --step data_directory
blc doctor --step data_directory

# Skip historical session indexing during quick smoke tests
blc install codex --no-history
blc status --no-history
```

### Exit Codes Contract
- `0`: Requested checks passed, or installation completed successfully.
- `1`: Check failed, prerequisite missing, or system service error.
- `2`: Usage error, invalid option, or unknown step ID.

### Output Flags
- `--json`: Emits machine-readable JSON reports (ideal for CI and subagent tooling).
- `--no-color`: Disables ANSI escape codes and progress animations for plain terminal logging.

---

## 4. Full Integration & System Acceptance Tests

By default, `pytest` uses isolated storage and simulated service managers. The package workflow tests these environments on every pull request and push to `main`:

| Environment | Python versions | Coverage |
| --- | --- | --- |
| Ubuntu 24.04, x86_64 and ARM64 | 3.10, 3.11, 3.12, 3.13, 3.14 | Built wheel, full default suite; real systemd lifecycle and CPU embeddings on 3.13 |
| macOS, Apple Silicon (`macos-latest`) | 3.10, 3.11, 3.12, 3.13, 3.14 | Built wheel, full default suite, real LaunchAgent lifecycle |
| Debian 12 (bookworm), amd64 and arm64 | Debian's Python 3.11 | Built wheel, full default suite and real CPU embeddings, as an unprivileged user |
| Debian 13 (trixie), amd64 and arm64 | Debian's Python 3.13 | Built wheel, full default suite and real CPU embeddings, as an unprivileged user |

The ARM64 jobs use native ARM64 runners. Debian jobs use Debian containers on those Linux runners: they exercise Debian's Python and libraries, but share the Ubuntu host kernel and do not run a systemd user manager. A Debian machine still needs a working `systemctl --user` session for onboarding. [Issue #45](https://github.com/gnulnx/Context/issues/45) tracks Debian machine-level lifecycle and headless robot startup acceptance. Python prereleases and free-threaded builds are not part of the supported test matrix.

To run live user-service tests or download real embedding models:

```bash
# Run unit tests only
python -m pytest -q

# Opt-in: Run live systemd user session lifecycle tests
BLCTX_SYSTEMD_TEST=1 python -m pytest -q tests/test_service.py

# Opt-in: Run real macOS LaunchAgent lifecycle tests from a desktop login
BLCTX_LAUNCHD_TEST=1 python -m pytest -q tests/test_launchd_service.py -k real_launchd

# Opt-in: Run real embedding downloads and vector indexing tests
BLCTX_INDEX_TEST=1 python -m pytest -q tests/test_index.py

# Opt-in: Full acceptance test suite (Systemd + Live Model + MCP + Hooks)
BLCTX_INDEX_TEST=1 BLCTX_SYSTEMD_TEST=1 python -m pytest -q
```

The macOS acceptance test loads only a unique installation-scoped LaunchAgent, verifies identity and health, repeats installation, kills the daemon to verify crash recovery, then uninstalls twice while checking data preservation. It covers spaces in HOME and the Python environment path, plus removal when the plist is already missing. Run it separately from the default suite: `BLCTX_LAUNCHD_TEST=1` intentionally enables the actual user manager. Model/MCP/capture integration tests also run on macOS with `BLCTX_INDEX_TEST=1`.

For local package development on either platform:

```bash
uv tool install --python 3.11 --editable .
blc uninstall codex
blc install codex
```

Source edits make the running daemon stale. Rerun `blc install codex --step background_service` after editing runtime code. This restarts the daemon only when necessary; repeat installs with unchanged code preserve its PID.

---

## 5. Codex Transcript Exploration (`blc explore`)

`blc explore` is a zero-side-effect, read-only diagnostic tool for inspecting local Codex transcripts before ingesting them:

```bash
# List 20 newest transcript paths (active and archived)
blc explore

# List up to 100 transcripts
blc explore --limit 100

# Inspect conversation turns in a specific transcript
blc explore /path/to/session.jsonl --limit 3

# View classified records (index, excluded, duplicate, metadata, unclassified)
blc explore /path/to/session.jsonl --view preview --limit 20

# View raw JSONL record types, line numbers, and byte offsets
blc explore /path/to/session.jsonl --view raw --limit 5

# Deep turn details with exclusion breakdown
blc explore /path/to/session.jsonl --details --limit 5
```

---

## 6. MCP Protocol & Tool Specifications

Base Layer Context exposes 8 tools over stdio MCP. Each response has one JSON text content block and no duplicate `structuredContent`:

| Tool | Mode | Parameters | Description |
| :--- | :---: | :--- | :--- |
| `context_status` | Read | None | Reports coverage, freshness, running jobs, and pending note embeddings. |
| `work_overview` | Read | `since`, `until`, `project`, `limit`, `offset` | One representative evidence package per project; three days by default, pagination by project. |
| `get_tag` | Read | `tag`, `limit`, `offset` | Exact case-sensitive global handoff lookup, newest first. |
| `recent_context` | Read | `since`, `until`, `project`, `limit` (1–50), `offset` | Retrieves recent turns with source citations (defaults to last 72 hours). Global by default. |
| `search_context` | Read | `query` (1–2000 chars), `since`, `until`, `project`, `limit`, `offset` | Hybrid semantic vector search with lexical fallback during synchronization. |
| `get_context` | Read | `context_id`, `limit`, `offset`, `char_offset` | Expands a specific turn or message into full text with pagination. |
| `open_session` | Write | `binding_key` (UUID), `project`, `parent_session_id`, optional `agent` | Initializes or resumes an authored session binding; producer is provenance only. |
| `log_update` | Write | `session_id`, `update_id`, `text`, `tags`, `kind`, `authorship`, `source_text` | Persists an idempotent, tagged authored note to SQLite before async embedding. |

All normal responses are limited to 24,000 serialized JSON bytes, including evidence, provenance, and coverage. The MCP wrapper stays below 48,000 bytes even after escaping that JSON. `context_status` reports compact counts; full operator diagnostics remain available explicitly through `blc index-status --details` or `blc index-status JOB_ID`. Do not feed full diagnostic output into normal recall.

Overviews deduplicate repeated text and select up to three excerpts per project, preferring authored notes and final outcomes and spreading representatives across sessions. They are extractive, not generated summaries. Search merges a bounded window of lexical and semantic candidates before pagination, prefers exact relevant evidence, and orders equally relevant authored facts newest first. Semantic failures fall back to lexical recall. `candidate_limit_reached` requests refinement instead of claiming exhaustive search.

Use `next_offset` to continue result pages. Search/overview excerpts retain exact character offsets and a `message_offset` for expansion with `get_context(limit=1)`. `next_char_offset` continues long messages. `coverage_incomplete`, omitted-project/item counts, and truncation flags describe limits before any external host truncation.

Conversation retention is seven days across producers; authored memory is durable. The daemon prunes at startup and every minute when its write worker is available. Read filters enforce expiry immediately even while maintenance is queued. Vector tombstones survive interruption, and SQLite validates semantic results. Reimport skips expired conversation messages. Source files and stable session bindings are preserved. SQLite reuses freed pages; retention is not secure erasure or an immediate filesystem-size reduction. Unknown legacy timestamps receive one seven-day window on migration; imports use the source's first imported modification time when a message timestamp is unavailable. Appending to a transcript does not renew its old undated messages.

### V1 acceptance and local review

From a checkout of the PR branch:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests

# Real CPU embeddings, capture/restart, MCP, and vector expiry/reimport:
BLCTX_INDEX_TEST=1 .venv/bin/python -m pytest -q
```

`tests/test_memory_v1.py` covers acceptance A–F: uneven project volume, latest saved values across sessions, focused LiDAR evidence, exact handoffs, oversized history/diagnostics/Unicode, and mixed Codex/Claude/Gemini provenance. The default suite starts a real isolated daemon and separate stdio clients, checks immediate persistence with no model, and measures the complete MCP result. The opt-in suite additionally checks actual vector deletion and reimport without resurrection. Tests isolate their storage and do not alter the installed user's memory.

To review the product in your normal Codex installation:

```bash
uv tool install --python 3.11 --editable . --force
blc install codex
blc doctor
blc overview --days 3
```

Keep the checkout and Python environment available to the background service. The installer upgrades the owned skill and restarts stale runtime code. Start a fresh Codex session so it discovers the new MCP tools and skill; review/trust Context handlers in `/hooks` if prompted.

In that session, ask “What have we worked on over the last few days?”, then save “Remember that the magic word is Alpha.” Open a separate session in a different repository and ask “What is the most recent magic word?” Save Beta there and verify a third session recalls Beta. Repeat with an ordinary fact from your own work. After doing some work, say “Tag the current work with test-handoff”; from another session say “Refresh context from tag test-handoff.” Expect a compact handoff with the next step. `blc tag test-handoff` gives a direct CLI check.

Installation initially imports five recent sessions; hooks capture subsequent work. Overview coverage describes representatives and may be partial. A fresh machine with no retained conversations should still install successfully, then demonstrate memory through explicit saves and new work. Claude/Gemini integrations are outside V1; mixed producer tests verify the shared store contract only.

---

## 7. Storage Layout & Permission Specifications

Linux defaults follow XDG conventions:

| Component | Default Path | Mode | Permissions | Content |
| :--- | :--- | :---: | :---: | :--- |
| **Data** | `~/.local/share/bl-context/` | `0700` | `drwx------` | SQLite database (`context.db`, mode `0600`) and Qdrant collection vectors (`vectors/`). |
| **Config** | `~/.config/bl-context/` | `0700` | `drwx------` | Systemd service definition (`blctxd-<UUID>.service`). |
| **Cache** | `~/.cache/bl-context/` | `0700` | `drwx------` | Pinned FastEmbed model files (`BAAI/bge-small-en`). |
| **State** | `~/.local/state/bl-context/` | `0700` | `drwx------` | Installation manifest (`installation.json`) and private Unix socket (`daemon.sock`, mode `0600`). |

macOS uses `~/Library/Application Support/bl-context/{data,config,state}` and `~/Library/Caches/bl-context`. The state directory also holds the private `daemon.log`, retained on ordinary uninstall and removed with `--purge`. Absolute XDG overrides work on both platforms. See the README for exact directory overrides and the Unix socket path limit.

### Platform and provider boundaries

`service.py` selects the `systemd_service.py` or `launchd_service.py` backend. Both expose `install`, `verify`, and `uninstall`; shared IPC and readiness polling live in `service_runtime.py`. Neither backend depends on Codex or a model API. `storage.environment()` carries resolved locations to services and agent integrations without relying on the host's shell environment.

`installers.py` retains the independent provider adapter boundary. A future Claude, Gemini, or local-model integration can reuse the service/storage lifecycle while adding its own configuration, transcript discovery, and capture adapters. This change adds macOS support for the existing Codex integration; it does not add those providers yet.

The macOS backend writes a mode `0600` plist in `~/Library/LaunchAgents`, uses `launchctl bootstrap` in `gui/<uid>`, starts at login, and restarts on failure. Startup waits for the private IPC identity and verifies it against the loaded service PID, installation ID, interpreter, and runtime fingerprint. Shutdown waits for deregistration and release of the writer lock before removing the owned plist/socket. Modified definitions and foreign registrations are preserved. An SSH-only session without a GUI login receives an actionable diagnostic.

Design references: [Apple's LaunchAgent lifecycle](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html), the installed `launchctl(1)` manual, and [Codex hook trust](https://developers.openai.com/codex/hooks). `launchctl print` has no guaranteed machine-readable format, so its small property parser fails closed if required identity fields are absent.

---

## 8. Uninstallation & Purge

```bash
# Standard uninstall: stops user service, unregisters MCP, removes hooks, retains data
blc uninstall codex

# Complete purge: stops services, removes all owned files and databases (preserves Codex transcripts)
blc uninstall codex --purge
```
