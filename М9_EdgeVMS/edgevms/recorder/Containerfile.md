# Containerfile — the recorder-worker image: Debian, GStreamer from apt, the Python deps from pip

**Role.** Builds `localhost/recorder-worker:latest` for `quadlet/worker.container`. GStreamer and PyGObject must come from the OS packages (there is no pip wheel for the plugins), so the base is Debian bookworm-slim and only the pure-Python dependencies come from pip. The default command is the Worker; the same image can run `tools/provision.py` because `tools/` and `migrations/` are copied too.

## Instruction by instruction
- `FROM docker.io/library/debian:bookworm-slim` — the same suite as the bench (`build-disk.sh` `SUITE=bookworm`), so GStreamer versions match what `tests/test_stall.py` would see on the box.
- `RUN apt-get install … python3 python3-pip python3-gi gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gir1.2-gst-plugins-base-1.0` — PyGObject (`gi`), the typelibs, and the plugin sets that provide `rtspsrc`, `rtph264depay`, `h264parse`, `splitmuxsink`, `mp4mux` (base/good) and `watchdog` (bad). `--no-install-recommends` and the `apt/lists` cleanup keep the image small.
- `WORKDIR /app` — `python3 -m worker` runs with `/app` on `sys.path`, which is what lets `worker/worker.py` do `from console.app import create_app` and `console/app.py` do `from worker.reconciler import …`.
- `COPY pyproject.toml .` — copied for reference; the next line does not install from it.
- `RUN pip3 install --break-system-packages --no-cache-dir asyncpg fastapi uvicorn argon2-cffi cryptography` — the five runtime dependencies listed in `pyproject.toml`, installed into the system interpreter (Debian's PEP 668 guard needs `--break-system-packages`), unpinned.
- `COPY worker worker`, `COPY console console`, `COPY migrations migrations`, `COPY tools tools` — the package. `worker.py` computes `MIGRATIONS` relative to its own file, so `/app/migrations` is found. Tests are not copied.
- `CMD ["python3", "-m", "worker"]` — `worker/__main__.py` → `asyncio.run(main())`.

## Notes
- The dependency list is duplicated between this file and `pyproject.toml`, and only `pyproject.toml` carries version floors; a `pip install .` would have kept them in one place.
- `gir1.2-gst-rtsp-server-1.0` is not installed, so `tools/fake_camera.py` cannot run inside this image; it is meant for the bench host.
