To record the installer GIF, run from this directory:

```bash
./make-demo-gif
```

The script records the installer with VHS, then converts the MP4 at native
resolution to full, opaque GIF frames. The final card remains in scrollback with
the shell prompt underneath it.

The GIF must be below 10,000,000 bytes. An oversized recording fails without
replacing the previous GIF. If needed, reduce `GIF_FPS` (default: 6) or shorten
the holds in `demo.tape`; `GIF_COLORS` defaults to 192.

The separate recall demo uses two real Codex sessions in a split terminal:

```bash
cd ..
.venv/bin/python assets/make-recall-gif
```

This creates `assets/recall.gif` for the README and `assets/recall.mp4` for social
posts. It requires an installed Context embedding model, a signed-in Codex CLI,
tmux, VHS, and FFmpeg. The recorder stages the shipped Context skill, hooks, and
MCP server in a disposable Codex home with an empty Context database. It copies
only the login and model weights; the first conversation is captured live.
Personal history and the running Context installation are left alone. The
temporary login, demo store, daemon, and tmux server are removed after recording.

`recall.tape` records directory trust, **Trust all and continue**, the first
message, and a fresh session calling `base-layer-context.search_context` before
answering **Base**. Only the prompts are scripted; Codex responses and tool calls
are live. The terminal switches panes with the tmux keyboard shortcut. The
installed Codex model is used with low reasoning effort for a short demo.

Recall uses 3 GIF frames per second by default and the same opaque-frame encoder
and 10 MB limit as the installer. Override `GIF_FPS` or `GIF_COLORS` when needed.
Both MP4 files retain the full recording frame rate; MP4 output is ignored by Git.
