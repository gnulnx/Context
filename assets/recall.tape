Output recall.mp4

Require tmux
Require codex

Set Shell "bash"
Set Theme "GitHub Dark"
Set FontFamily "JetBrains Mono"
Set FontSize 18
Set Width 1600
Set Height 900
Set Padding 20
Set WindowBar Colorful
Set WindowBarSize 32
Set Framerate 30
Set CursorBlink true
Set TypingSpeed 65ms

# Run with ./assets/make-recall-gif. It prepares two real Codex sessions and
# an empty, isolated Context store before attaching the split terminal here.
Hide
Type 'tmux -L "$RECALL_DEMO_SOCKET" attach-session -t recall'
Enter
Sleep 1s
Show
Sleep 1s

Type "codex"
Sleep 500ms
Enter
Wait+Screen@30s /Yes, continue/
Sleep 1s
Enter
Wait+Screen@30s /Trust all and continue/
Sleep 2s
Down
Sleep 750ms
Enter
Wait+Screen@30s /for shortcuts/
Sleep 750ms

Type 'The magic word is "Base".'
Sleep 500ms
Enter
Wait+Screen@120s /• .*Base/
Sleep 3s

# Switch to a fresh session while keeping the first conversation visible.
Ctrl+b
Right
Sleep 750ms
Type "codex"
Sleep 500ms
Enter
Wait+Screen@30s /(?s)OpenAI Codex.*OpenAI Codex/
Sleep 2s
Type "What was the magic word? Reply with just the word."
Sleep 500ms
Enter
Wait+Screen@120s /(?s)Called.*base-layer-context.search_context/
Wait+Screen@120s /│ *• Base/
Sleep 6s
