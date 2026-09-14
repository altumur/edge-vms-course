# worker.container — the recorder Worker: reconcile, pump_buses, report, retention and the console in one process

**Role.** Lessons 6–9. Quadlet unit at `/etc/containers/systemd/worker.container`, generated into `worker.service`, which `health/rauc-mark-good.service` orders after. Runs `recorder/Containerfile`'s image, whose `CMD` is `python3 -m worker` (`recorder/worker/__main__.py`). Ordering against the database is expressed in unit dependencies, not with sleep (Lesson 5).

## Stanza by stanza
### `[Unit]`
- `Description=Recorder Worker`.
- `Requires=postgres.service`, `After=postgres.service` — the worker is not started without the database unit and starts after it. `Requires` also stops the worker when Postgres is stopped.

### `[Container]`
- `Image=localhost/recorder-worker:latest` — built from `recorder/Containerfile`.
- `Volume=/data/archive:/data/archive:z` — the archive: `pipeline.segment_dir()` writes `<ARCHIVE_DIR>/<camera>/e<epoch>/<start>Z.mp4`, `retention.py` unlinks there, and the health check's М10 branch looks under `/data/archive/vms`.
- `Volume=/data/config:/data/config:ro,z` — read-only access to `column.key` (`COLUMN_KEY_FILE`), which `tools/provision.py key` writes from outside.
- `EnvironmentFile=/data/config/worker.env` — `worker.env.example`: `DATABASE_URL`, `ARCHIVE_DIR`, `SEGMENT_SECONDS`, the disk-full policy, the console address. Hand-provisioned; the stand-in for what М12 resolves.
- `Network=host` — the worker reaches Postgres on `127.0.0.1:5432` and binds the console on `127.0.0.1:8080` without port mapping; RTSP sources on the LAN are reachable directly. `bench/boot.sh` forwards host 8080 to that port.
- `StopTimeout=20` — SIGTERM, then twenty seconds before SIGKILL. The comment says SIGTERM lets every `splitmuxsink` finalize its open segment; the kill test in Lesson 8 uses SIGKILL on purpose and loses exactly that segment.

### `[Service]`
- `Restart=always`, `RestartSec=2` — a crash (including a failed first connect to Postgres) is retried after two seconds; the reconciler re-derives `actual` from the database on every start, so a restart is cheap.

### `[Install]`
- `WantedBy=multi-user.target`.

## Notes
- The `StopTimeout` comment and `recorder/README.md` disagree: the README's "Known gaps" says `CameraPipeline.stop()` sends EOS and goes to NULL without waiting for the muxer to finalize, "so `SIGTERM` therefore behaves like the lesson's `SIGKILL` for the open segment". The twenty seconds are budget the code does not currently use.
- `bench/build-disk.sh` does not bake this unit or `postgres.container` into the image; they are added on the bench.
