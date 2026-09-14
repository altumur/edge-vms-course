# fake_camera.py — an RTSP camera you can unplug and stall without hardware

**Role in the module.** Lesson 8's test fixture: a `gst-rtsp-server` that serves `rtsp://127.0.0.1:<port>/cam0 … cam<N-1>`, each an H.264 test pattern, and that on `SIGUSR1` stalls every stream with the sockets held open — no buffers flow, the TCP connection does not close — which is exactly the failure the `watchdog` element exists for. `SIGUSR2` resumes. Started by the README's bench recipe (`--port 8554 --count 50`) and by `tests/test_stall.py`. Needs `gir1.2-gst-rtsp-server-1.0`; listed in the README as written, not executed. Not runnable inside the recorder image (no rtsp-server typelib there).

## Module-level names
- `LAUNCH` — the per-mount pipeline: `videotestsrc is-live=true pattern=ball` at 640×360/25 fps → `valve name=valve drop=false` → `x264enc tune=zerolatency key-int-max=25 speed-preset=ultrafast` → `rtph264pay name=pay0 pt=96`. The `valve` is the stall switch; `key-int-max=25` gives a keyframe every second so a recorder reconnecting gets pictures fast.

## `class Factory(GstRtspServer.RTSPMediaFactory)`
One factory per mount point, sharing a list of valves with `main()`.
### `__init__(self, valves)` — `set_launch(LAUNCH)`, `set_shared(True)` (all clients of a mount share one pipeline), and connect `media-configure`.
### `_configured(self, factory, media)` — when the media pipeline is built for a client, find its `valve` element and append it to the shared list, so the signal handlers can reach every live stream.

## Functions
### `main()`
Parses `--port` (8554) and `--count` (1). Creates an `RTSPServer` on that port, adds `Factory(valves)` at `/cam0` … `/cam{count-1}`, attaches to the default GLib context. Defines `stall()` (set `drop=True` on every valve, print `STALLED n streams`) and `resume()` (`drop=False`, print `RESUMED`), both returning `True` so GLib keeps the handler installed, and registers them with `GLib.unix_signal_add` for `SIGUSR1`/`SIGUSR2`. Prints the URL range and runs the `GLib.MainLoop` forever.

## Notes
- The stall is global: every stream stops on `USR1`. The README calls a per-stream stall "a small change to the valve wiring and a good exercise", and `test_stall.py` works around it by stalling everything and asserting the watchdog fired on all pipelines.
- Valves are collected only when a client connects (`media-configure`), so a `USR1` before any recorder has connected stalls nothing.
