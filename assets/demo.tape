Output demo.gif

Set Shell "bash"
Set Theme "GitHub Dark"
Set FontFamily "JetBrains Mono"
Set FontSize 14
Set Width 1200
Set Height 675
Set Padding 20
Set WindowBar Colorful
Set WindowBarSize 32
Set Framerate 30
Set CursorBlink false

# Reset the integration so every recording follows the complete install path.
Hide
Type "export PATH=$PWD/.venv/bin:$PATH PS1='> ' && blc uninstall codex --no-color >/dev/null 2>&1 && clear && printf 'DEMO_%s\\n' READY"
Enter
Wait+Screen@30s /DEMO_READY/
Type "clear"
Enter
Sleep 750ms
Show

# The only command users need to remember.
Set TypingSpeed 50ms
Sleep 1s
Type "blc install codex"
Sleep 500ms
Enter

# Let viewers read the optional hooks disclosure, then accept the default.
Wait+Screen@60s /Automatic Codex capture/
Sleep 2s
Enter

# Hold the completed installer long enough to read before encoding.
Wait+Screen@60s /Ready\./
Sleep 3s
