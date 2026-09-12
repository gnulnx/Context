# Launch videos

Three separate social edits of the existing installer and recall recordings:

- **The magic trick (console revision):** one opening line on charcoal, visible hook approval, full terminal panes, the real recall response, and a plain GitHub end card.
- **The brand introduction:** fade and glow on the name, slide in the promise, then show recall.
- **Install. Tell it. Ask again.:** follow the integration from installation through recall.

From the repository root:

```bash
.venv/bin/python assets/make-social-videos
```

Requires FFmpeg, ffprobe, fontconfig (`fc-match`), Pillow, and NumPy. Rendering
uses Lato and JetBrains Mono when installed; fontconfig supplies fallbacks.
The recorder scripts produce the two inputs: `assets/demo.mp4` and
`assets/recall.mp4`. This compositor only reads them. The README GIFs are never
rewritten and no Codex sessions or installers are launched during composition.

Open `output/index.html` to compare and download the three MP4s. Each export is
1080×1080, 30 fps, H.264/yuv420p with silent AAC and a fast-start MP4 index. These
settings use the formats documented in [X's video guide](https://help.x.com/en/using-x/x-videos)
and [Media Studio specifications](https://help.x.com/en/using-x/media-studio-faqs).
The renderer checks duration and size, then decodes the complete output before
replacing the previous file. Generated videos, posters, and the preview page
are ignored by Git.

To render just one cut:

```bash
.venv/bin/python assets/make-social-videos --only 01-magic-trick
```

The console revision keeps one steady crop through each conversation, leaves
space above the native pane title, and shows the recorded “Hooks need review”
prompt and “Trust all and continue” selection for five seconds after the opening
line. It then holds the Codex welcome screen before typing. It has no numbered
captions, outer display box, or separate “New
session” card. Rendering one cut preserves the other videos and their gallery
entries. Preview URLs include a content revision so a changed MP4 reloads.
The recorded “Base” answer holds for about two seconds before the final card.
The final card holds for seven seconds with a larger name, terminal-blue
“Context,” and a smaller grey tagline and GitHub URL. It stays fully visible
through the final frame.

`storyboards.json` controls the copy, title cards, source time ranges, crops,
and playback speeds. Terminal close-ups come from real recordings; waits are
shortened. The brand and install cuts use a large **Base.** heading to emphasize
the recorded answer; the magic trick keeps the native terminal response. Title
cards are animated typography, not simulated terminal responses. If you
record new source demos, review their timestamps and update the storyboard
before rendering. `output/manifest.json` records source hashes and the complete
edit timeline for each export.

The intended post text is simply `https://github.com/gnulnx/Context` with one
attached video. These commands render local previews; they do not post to X.
