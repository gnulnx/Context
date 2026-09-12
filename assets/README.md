To make the demo gif animation run:

./make-demo-gif

The script records the installer with VHS, then converts the MP4 at native
resolution to full, opaque GIF frames. The final card remains in scrollback with
the shell prompt underneath it.

The GIF must be below 10,000,000 bytes. An oversized recording fails without
replacing the previous GIF. If needed, reduce `GIF_FPS` (default: 6) or shorten
the holds in `demo.tape`; `GIF_COLORS` defaults to 192.
