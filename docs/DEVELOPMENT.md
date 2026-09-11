# Base Layer Context — Developer & Contributor Guide

This guide covers local development, testing harnesses, step-by-step installation verification, and low-level CLI development flags for Base Layer Context (`blctx`).

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
2. `background_service`: Registers and starts `blctxd` systemd user service.
3. `codex_mcp`: Registers stdio MCP server in `$CODEX_HOME/config.json`.
4. `codex_skills`: Installs `base-layer-context` skill into `$CODEX_HOME/skills`.
5. `embedding_model`: Downloads & cryptographically verifies `BAAI/bge-small-en` (384-dim CPU).
6. `session_discovery`: Inspects `$CODEX_HOME/sessions` and selects the newest 5 sessions.
7. `history_index`: Indexes selected sessions into local SQLite and Qdrant vector storage.
8. `service_health`: Verifies IPC communication and exclusive daemon database locks.
9. `mcp_health`: Spawns stdio MCP subprocess, verifies 6 tools, and tests protocol round-trip.
10. `history_retrieval`: Runs end-to-end semantic search and context expansion through MCP.
11. `codex_hooks`: Registers `SessionStart`, `Stop`, and `SessionEnd` hooks in `$CODEX_HOME/hooks.json`.

---

## 3. Focused Step Execution & Diagnostics

You can run individual steps for focused testing and development using `--step`:

```bash
# Test background service installation and verification
blctx install codex --step background_service --json
blctx status --step background_service --json
blctx doctor --step background_service --no-color

# Test data directory creation and schema integrity
blctx install codex --step data_directory
blctx doctor --step data_directory

# Skip historical session indexing during quick smoke tests
blctx install codex --no-history
blctx status --no-history
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

By default, `pytest` runs isolated unit tests with mock paths and simulated services. To run live systemd user service tests or download real embedding models:

```bash
# Run unit tests only
python -m pytest -q

# Opt-in: Run live systemd user session lifecycle tests
BLCTX_SYSTEMD_TEST=1 python -m pytest -q tests/test_service.py

# Opt-in: Run real embedding downloads and vector indexing tests
BLCTX_INDEX_TEST=1 python -m pytest -q tests/test_index.py

# Opt-in: Full acceptance test suite (Systemd + Live Model + MCP + Hooks)
BLCTX_INDEX_TEST=1 BLCTX_SYSTEMD_TEST=1 python -m pytest -q
```

---

## 5. Codex Transcript Exploration (`blctx explore`)

`blctx explore` is a zero-side-effect, read-only diagnostic tool for inspecting local Codex transcripts before ingesting them:

```bash
# List 20 newest transcript paths (active and archived)
blctx explore

# List up to 100 transcripts
blctx explore --limit 100

# Inspect conversation turns in a specific transcript
blctx explore /path/to/session.jsonl --limit 3

# View classified records (index, excluded, duplicate, metadata, unclassified)
blctx explore /path/to/session.jsonl --view preview --limit 20

# View raw JSONL record types, line numbers, and byte offsets
blctx explore /path/to/session.jsonl --view raw --limit 5

# Deep turn details with exclusion breakdown
blctx explore /path/to/session.jsonl --details --limit 5
```

---

## 6. MCP Protocol & Tool Specifications

Base Layer Context exposes 6 tools over stdio MCP:

| Tool | Mode | Parameters | Description |
| :--- | :---: | :--- | :--- |
| `context_status` | Read | None | Reports coverage, freshness, running jobs, and pending note embeddings. |
| `recent_context` | Read | `since`, `until`, `project`, `limit` (1–50), `offset` | Retrieves recent turns with source citations (defaults to last 72 hours). Global by default. |
| `search_context` | Read | `query` (1–2000 chars), `since`, `until`, `project`, `limit`, `offset` | Hybrid semantic vector search with lexical fallback during synchronization. |
| `get_context` | Read | `context_id`, `limit`, `offset`, `char_offset` | Expands a specific turn or message into full text with pagination. |
| `open_session` | Write | `binding_key` (UUID), `project`, `parent_session_id` | Initializes or resumes an authored session binding. |
| `log_update` | Write | `session_id`, `update_id`, `text`, `tags`, `kind`, `authorship`, `source_text` | Persists an idempotent, tagged authored note to SQLite before async embedding. |

---

## 7. Storage Layout & Permission Specifications

All data is stored strictly in user-scoped Linux directories respecting XDG specifications:

| Component | Default Path | Mode | Permissions | Content |
| :--- | :--- | :---: | :---: | :--- |
| **Data** | `~/.local/share/bl-context/` | `0700` | `drwx------` | SQLite database (`context.db`, mode `0600`) and Qdrant collection vectors (`vectors/`). |
| **Config** | `~/.config/bl-context/` | `0700` | `drwx------` | Systemd service definition (`blctxd-<UUID>.service`). |
| **Cache** | `~/.cache/bl-context/` | `0700` | `drwx------` | Pinned FastEmbed model files (`BAAI/bge-small-en`). |
| **State** | `~/.local/state/bl-context/` | `0700` | `drwx------` | Installation manifest (`installation.json`) and private Unix socket (`daemon.sock`, mode `0600`). |

---

## 8. Uninstallation & Purge

```bash
# Standard uninstall: stops systemd service, unregisters MCP, removes hooks, retains data
blctx uninstall codex

# Complete purge: stops services, removes all owned files and databases (preserves Codex transcripts)
blctx uninstall codex --purge
```
