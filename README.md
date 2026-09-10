# Base Layer Context

Persistent context for coding agents.

This repository contains the Python package, onboarding CLI, and the first
local installation, history retrieval, and Codex MCP integration.
Lifecycle capture and tagged updates are available for local testing; full onboarding acceptance remains under development.

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
and tested on Linux, along with the systemd user service and Codex MCP
registration; the remaining integration checks report failure.
Full installation starts the Context user service and registers its MCP server.
History indexing is explicit; model download occurs on first embedding use.
The historical-recall/update skill and lifecycle hooks are installed. Codex requires a separate user trust review before hooks run.
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

### Explore Codex transcripts before importing

```sh
blctx explore                         # newest 20 transcript paths, active and archived
blctx explore --limit 100
blctx explore /absolute/path/to/session.jsonl
blctx explore /absolute/path/to/session.jsonl --view raw --limit 5
blctx explore /absolute/path/to/session.jsonl --start-line 100 --limit 20 --json
blctx explore --root /another/codex/home
```

This read-only command reads local JSONL files, not the Codex App Server. It does
not create Context state, import records, start services, or resume sessions.
The `--view messages` view shows full candidate user/assistant records, including
injected context; it deliberately does not claim these are approved import data.
Raw view exposes metadata, events, tools, and other record types. Results include
source line/byte positions and a next-line hint. Malformed JSON is shown as a
parse error; reads stop at the file size captured when opened. `--limit` limits
record count in raw/messages views and turn count in preview, not text size. JSON escapes embedded terminal control
characters. Transcript contents may contain sensitive text; inspect locally.

Preview proposed ingestion decisions before importing:

```sh
blctx explore /path/to/session.jsonl --view preview --limit 100
blctx explore /path/to/session.jsonl --view preview --start-line 101 --json
```

Preview groups displayed source records by explicit turn ID (or an `unassigned`
group when absent). It labels `index`, `context_only`, `excluded`, `duplicate`,
`metadata`, and `unclassified`, with reasons and original line/byte positions.
Default preview shows selected conversation text and compact exclusion totals.
Use `--details` for per-record decisions and turns without selected messages.
Preview limits count complete turns, and counts cover the whole captured file.
The captured file is classified before pagination so duplicate matching and
turn context remain consistent; large files therefore require a full scan. Exact cross-representation matches
within a turn are proposed duplicates; same-representation repeated messages are
retained. These are review heuristics, not an approved importer: unknown phases
and shapes remain unclassified, and injected-context detection uses known prefixes.
No records are saved or indexed. Use raw view at a source line to investigate.


Conversation-first exploration is the default when selecting a session:

```sh
blctx explore /path/to/session.jsonl --limit 3
blctx explore /path/to/session.jsonl --details --limit 3
blctx explore /path/to/session.jsonl --json --limit 3
```

JSON `turns[].messages` contains the exact proposed searchable text, roles,
source locations and duplicate references. `--details` adds all classified
records. No synthetic summaries replace source text. Turns without a selected
request or answer are explicitly flagged. Classification lives separately in
`preview.classify_transcript` so preview and the importer share the same selection policy.

### Index and query local history

The first retrieval implementation uses **BAAI/bge-small-en** through FastEmbed
on CPU, with Qdrant local storage owned by `blctxd`. SQLite stores normalized
messages, provenance, import checkpoints, durable jobs, and rebuildable vectors.
The tested Qdrant/FastEmbed versions are pinned. The actual model/tokenizer
fingerprint is recorded to prevent silently mixing different embeddings.

```sh
source .venv/bin/activate
blctx install codex --step background_service

# Start with one transcript you reviewed in explore:
blctx index /absolute/path/to/session.jsonl --wait

# Or explicitly import all active/archived transcripts under the Codex root:
blctx index --wait
# blctx index --root /another/codex/root --wait

blctx index-status
blctx recent --days 3
blctx recent --since 2026-09-08 --until 2026-09-11 --project /absolute/project/path
blctx search 'why did we change the deployment approach?'
blctx context CONTEXT_ID
```

These commands emit JSON. Index requests return a durable job ID immediately;
`--wait` polls for up to ten minutes (progress goes to stderr). If it times out,
the job keeps running: inspect it with `blctx index-status JOB_ID`. First indexing
use downloads the model into Context's private cache. Embedding inference is
local; no transcript text is sent to an embedding API.

The importer uses the same selection policy as `explore --view preview`:
user requests, visible progress commentary, and final answers are embedded,
and exclusions stay out of the index. Unknown record shapes produce a **partial**
job with counts; a successful queue acknowledgement never claims completion.
Changed files are reparsed and reconciled; unchanged snapshots are skipped.
Completed files are checkpointed independently, and interrupted jobs replay at
daemon startup. Moving a known session into the archive updates source references
without duplicating the session. Conflicting same-ID files are reported.

Search chunks follow tokenizer boundaries (384 tokens with 48-token overlap).
Results include exact source text, session/turn IDs, project, timestamps, source
file/line/byte positions, and chunk character ranges. Recent results are grouped
by turn and filtered by message activity time. Dates without timezone offsets
mean UTC; `--since` is inclusive and `--until` exclusive. Unknown timestamps are
reported in coverage and do not match a date range.

Use `--limit` and `--offset` to page results. Responses cap returned text at
24,000 characters and explicitly flag truncation. Recent previews cap each turn
at 20 searchable messages; expand with `context`. For a long individual message,
use `blctx context CONTEXT_ID --offset N --limit 1 --char-offset M`, taking M from
`next_char_offset`. Semantic scores indicate similarity, not factual confidence.

Coverage describes only imported files, reports changed/missing sources and
partial imports, and includes recent job states. Querying does not automatically
capture new Codex activity by itself. Trusted lifecycle hooks register sessions for background capture; rerun `index` for other explicitly selected history. Full onboarding readiness remains
red for integrations not yet implemented.

Default uninstall stops the daemon and retains the index/model cache. `--purge`
removes recorded index/model artifacts and the database while preserving unknown
files. Original Codex transcripts remain untouched.

Validation:

```sh
python -m pytest -q
# Explicit integration tests download/use the real embedding model:
BLCTX_INDEX_TEST=1 python -m pytest -q tests/test_index.py
# Optional real systemd user-service lifecycle:
BLCTX_SYSTEMD_TEST=1 python -m pytest -q tests/test_service.py
```

### Codex MCP integration

```sh
blctx install codex --step codex_mcp
blctx status --step codex_mcp --json
blctx doctor --step codex_mcp --json
codex mcp get base-layer-context --json
```

Installation detects `codex` on PATH and uses `CODEX_HOME` (default `~/.codex`).
It registers the absolute Python interpreter, installation UUID, and explicit XDG
roots using `codex mcp add`. It preserves unrelated config and refuses a name
collision or a modified owned entry. Repeat installation is idempotent.
`blctx uninstall codex` removes the owned registration before stopping the service;
retained data alone does not satisfy the installed check.

Start a **new Codex conversation** after registration and ask it to use
`base-layer-context` to summarize work from the last few days. Import reviewed
transcripts first using `blctx index ... --wait`; registration does not import all
history. Six tools are available:

| Tool | Purpose |
| --- | --- |
| `context_status` | Inspect coverage, freshness, import jobs, pending note embeddings |
| `recent_context` | Retrieve activity in a time range (last 72 hours by default) |
| `search_context` | Semantic search with project and time filters |
| `get_context` | Expand source context, including commentary and paginated long text |
| `open_session` | Create/reopen a Context-owned session UUID |
| `log_update` | Persist an idempotent, tagged authored note |

`open_session` takes a caller-generated UUID `binding_key`, optional `project`,
and optional Context `parent_session_id`. Persist the binding key for a conversation
and reuse the same metadata on retry/resume. New conversations and forks get new
keys. Context generates and stores its own random session UUID; imported Codex
session IDs are provenance only. The SessionStart hook persists/injects this binding, and the installed skill guides tagged updates. MCP server
startup alone does not identify a Codex conversation.

For `log_update`, pass the returned `session_id`, a new UUID `update_id`, `text`,
and optional `tags`, `kind` (`status`, `decision`, `note`), and `authorship`
(`agent_generated`, `user_requested`). Reuse the same update UUID and content on
retry; conflicting reuse fails. Limits are 16,000 text characters, 20 tags, and
64 characters per tag. Tags are preserved in retrieved metadata; exact tag
filtering is not implemented yet. `user_requested` records an agent's attribution,
not independently verified user approval.

Notes are committed to SQLite before background embedding. Recent/context tools
can retrieve them immediately; semantic search includes them once indexed.
`context_status.authored_updates_pending` reports incomplete embeddings. Retry the
same `log_update` or restart the daemon to retry failed embeddings. Results label
notes as `source_type=authored_update` with `context_session_id`; transcript results
use `source_type=codex_transcript` and `source_session_id`. Imported text remains
source evidence, and authored notes retain their attribution.

The registered MCP entry point uses stdio; diagnostic logs go to stderr. Tool
errors report an unavailable daemon or mismatched/inactive installation. The
`codex_mcp` check verifies saved/effective registration; the separate `mcp_health`
onboarding step remains pending even though protocol integration tests run here.

```sh
# Full real model, daemon, Codex registration and systemd lifecycle acceptance:
BLCTX_INDEX_TEST=1 BLCTX_SYSTEMD_TEST=1 python -m pytest -q
```


### Codex historical-recall skill

```sh
blctx install codex --step codex_skills
blctx status --step codex_skills --json
blctx doctor --step codex_skills --json
```

The wheel bundles `src/bl_context/skills/base-layer-context/SKILL.md` as package
resources. Installation writes one skill to `$CODEX_HOME/skills/base-layer-context`
(default `~/.codex/skills/base-layer-context`). This path is tested with fresh
Codex 0.154.0 `skills/list` requests, including a custom root, removal, and reinstall.
No repository checkout is needed at runtime. Restart an existing Codex client if
its skill list is stale.

The installer records the version, root, exact file path, and installed SHA-256.
An unchanged owned file can be upgraded; edited files, unowned directories,
symlinks, and hardlinks are preserved with an error. A second copy in the standard
`~/.agents/skills` or `~/.codex/skills` user roots blocks installation rather than
creating duplicate discovery entries. Arbitrary repository/plugin copies are not
scanned globally. A disabled `skills.config` entry remains disabled and fails the
installed check.

`blctx uninstall codex` removes only the hash-matching installed skill, even if
the package version has since changed. Missing owned files are tolerated;
additional user files remain. Default uninstall retains Context data, but status
and doctor still fail after the skill is removed. Uninstall uses the recorded
root even when the current `CODEX_HOME` differs.

The skill teaches scoped history retrieval, timezone boundaries, source citations,
coverage limitations, and historical text as evidence. SessionStart binding and intelligent tagged updates are described below. Canonical-prompt behavioral
acceptance remains the final onboarding ticket; discovery tests do not claim it.


### Trusted lifecycle capture and tagged updates

```sh
blctx install codex --step codex_hooks
```

Use the same `blctx` installation/environment throughout this test: registration
contains its absolute interpreter path. Switching environments changes the hook
definition and can require another Codex review.

This registers three owned commands in `$CODEX_HOME/hooks.json`, installs the
skill/MCP prerequisites, and checks their actual state. **The first install exits
1 while Codex trust is pending.** In Codex, open `/hooks`, inspect the three Context
commands, and trust them. Start a new conversation afterward. Context never sets
trust, bypasses it, or marks a pending review green. Hook handler changes require
reinstallation and review of the changed command definition.

| Event | Action |
| --- | --- |
| SessionStart | Persist a binding UUID and instruct the agent to call `open_session` before work; reuse the binding after resume/compaction |
| Stop | Persist a deduplicated catch-up event; never request another model continuation |
| SessionEnd | Persist a final catch-up event; never hold the conversation open |

Hook handlers write only small SQLite records, with bounded lock waits and a
2-second Codex timeout. Errors return advisory output, allowing work to continue.
No model or embedding runs in a hook. The daemon checks registered transcript
snapshots every two seconds and reuses unchanged chunk embeddings. The transcript
is reparsed for classification; this first version does not implement a streaming
parser. No other history is automatically enrolled.

The Codex event ID is a lookup hint scoped to CODEX_HOME. Context generates its
own binding and session UUIDs. Different event session IDs (including forks) get
new bindings. Missing event IDs are rejected rather than guessed. A null
transcript path retains the event pending a usable path; capture cannot promise
transcript recovery for ephemeral/no-transcript conversations. `SessionEnd`
delivery does not cover crashes, so the daemon also reconciles registered files
without another hook event.

The skill saves visible progress with useful tags through `log_update`, using
`source_text` to associate an exact visible message with captured transcript
references. Matching is within a Context session; repeated identical source text
can share references. New summaries omit source_text and remain distinct authored
notes. Private thinking, raw tool traffic, and setup instructions are excluded.
User requests such as “index/tag our recent work” use this same skill and MCP.

Session/update storage acknowledgements bypass the embedding worker. SQLite WAL
mode prevents long recovery read snapshots from blocking those writes. Acknowledged
updates are durable and may still await semantic indexing. Existing imported
files retain their prior selection until explicitly reindexed; coverage reports
outdated selection. New capture includes visible progress by default.

Local smoke test, after trusting the hooks:

1. Start a **new** Codex conversation in a small test project.
2. Ask: “Give a short progress update about reviewing the sensor integration,
   save it with useful tags using Context, then tell me the Context session ID.”
3. Ask: “Index/tag our recent work with `sensors` and `review`.”
4. Ask Context to retrieve that work and inspect timestamps, tags, source
   references, and the distinction between a visible update and a summary.
5. Close the test conversation normally (or archive it). SessionEnd can be delayed
   while a conversation remains open in another client.
6. Run `blctx doctor --step codex_hooks --json`. Green requires all three event
   types to have arrived, indexed content, and no pending/failed capture. Inspect
   `blctx index-status` (`capture` field) for backlog, null paths, and errors.

Resume the test conversation and confirm the Context ID stays the same. Fork it
and confirm the new conversation receives a different ID. Compact and continue to
confirm the hook restores the existing binding. Subagent-specific lifecycle
hooks are not installed in this first version.

For uninstall review, use an isolated HOME/CODEX_HOME/XDG environment; do not
uninstall your working installation merely to run a test. `blctx uninstall codex`
removes owned hook groups, then the skill/MCP/service. It preserves other hook
entries and refuses modified Context commands. An old in-flight handler cannot
enqueue after removal, and capture checks installation generation before commit.
Reinstalling requires review of its new hook generation. Inline `[hooks]` config
is preserved with an actionable conflict; consolidate it into hooks.json first.

```sh
python -m pytest -q
BLCTX_INDEX_TEST=1 BLCTX_SYSTEMD_TEST=1 python -m pytest -q
```

Automated tests use isolated hook roots and never grant trust. They distinguish
synthetic handler/recovery tests from fresh Codex discovery and real trusted
client delivery. Full installer UX and the remaining onboarding checks are still
separate acceptance work.
