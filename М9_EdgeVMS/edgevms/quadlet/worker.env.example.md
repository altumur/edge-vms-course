# worker.env.example — the recorder Worker's environment on the data partition

**Role.** Lesson 5. Template for `/data/config/worker.env`, loaded by `EnvironmentFile=` in `quadlet/worker.container` and read by `recorder/worker/config.py::Settings`. Hand-provisioned at commissioning — the README calls it "the stand-in М12 resolves". Only a subset of `Settings` is listed; the rest (`POLL_INTERVAL`, `BUS_TICK`, `REPORT_INTERVAL`, `MAX_BACKOFF`, `STALL_FAILURES`, `RETENTION_INTERVAL`, `PARTITIONS_AHEAD`, `RTSP_LATENCY_MS`, `SESSION_TTL`, `LOG_LEVEL`) takes the defaults in `config.py`.

## Key by key
- `DATABASE_URL=postgresql://recorder:CHANGE-ME@127.0.0.1:5432/recorder` — asyncpg DSN for `PgStore.connect`; the password must match `pg.env` for `postgres.container`. Loopback because the worker runs with `Network=host`.
- `ARCHIVE_DIR=/data/archive` — root of the segment tree and the path `retention.py` measures with `statvfs`. Must be the same mount as the `Volume=` in `worker.container`.
- `COLUMN_KEY_FILE=/data/config/column.key` — the 32-byte AES-GCM key for `cred_secret` (`secrets.py`); `tools/provision.py key` creates it. Missing file: the worker starts with a warning and cameras with credentials cannot start.
- `EPOCH=1` — goes into every segment path (`e1/`) and every `segments.epoch` row. 1 and unused in М9; in М11 it becomes the fencing token.
- `SEGMENT_SECONDS=600` — `splitmuxsink max-size-time`; a product decision that bounds what a hard kill loses and how many index rows are written. `health/rauc-health-check` reads the same variable name to size its window; the two files are not linked, so they must be kept in agreement by hand.
- `WATCHDOG_MS=8000` — the `watchdog` element's timeout: no buffer for eight seconds posts an ERROR on the bus and the pipeline is torn down (a stalled camera with its socket still open).
- `DISK_FULL_POLICY=degrade_retention` — one of `stop_recording | degrade_retention | by_priority`; `Settings.__post_init__` rejects anything else at startup.
- `DISK_HIGH_WATER=0.95` — fraction of the archive filesystem used at which the policy is applied; the policy frees down to `0.90`.
- `CONSOLE_HOST=127.0.0.1`, `CONSOLE_PORT=8080` — where uvicorn serves `console/app.py`; loopback only, forwarded to the developer by `bench/boot.sh`, and read locally by the health check.
