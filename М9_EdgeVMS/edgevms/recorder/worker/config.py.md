# config.py — `Settings`: every knob of the recorder, read from the environment

**Role in the module.** Configuration comes from the environment (М8 Lesson 6); on the appliance the values arrive through `EnvironmentFile=/data/config/worker.env` (`quadlet/worker.env.example`), so credentials never live in the image. One frozen dataclass, constructed once in `worker.main()` (and by `tools/provision.py`, and by `tests/conftest.settings()` with overrides), passed to the `Worker`, the pipelines, `retention.py` and the console. Validates the one enumerated value at construction time so a typo fails at startup, not at the first full disk.

## Functions
### `_env(name, default)`
`os.environ.get` with a default; the only way any value enters.

## `class Settings` (frozen dataclass)
Every field is a `default_factory` reading its variable at instantiation, so the environment is sampled when `Settings()` is called, not at import.
- `database_url` — `DATABASE_URL`, default `postgresql://recorder@127.0.0.1:5432/recorder`; asyncpg DSN for `PgStore.connect`.
- `archive_dir` — `ARCHIVE_DIR`, default `/data/archive`; segment root and the filesystem `retention.py` measures.
- `column_key_file` — `COLUMN_KEY_FILE`, default `/data/config/column.key`; `ColumnKey.load` if it exists.
- `epoch` — `EPOCH`, default 1; in every segment path and index row.
- `segment_seconds` — `SEGMENT_SECONDS`, default 600. Lesson 7: segment length is a product decision — it bounds what a hard kill loses (Lesson 8, Step 4) and how many index rows are written. Also the orphan grace (`2 ×`) and the timeline widening.
- `watchdog_ms` — `WATCHDOG_MS`, default 8000; the `watchdog` element's timeout.
- `rtsp_latency_ms` — `RTSP_LATENCY_MS`, default 200; `rtspsrc latency=`.
- `poll_interval` — `POLL_INTERVAL`, default 2 s; the reconcile timer (correctness). `bus_tick` — `BUS_TICK`, default 0.2 s; how often every bus is drained. `report_interval` — `REPORT_INTERVAL`, default 5 s; status write-back, and ×3 is the console's "node reporting" window. `max_backoff` — `MAX_BACKOFF`, default 60 s; cap on the exponential base. `stall_failures` — `STALL_FAILURES`, default 3; failures before `LAGGING` becomes `STALLED`. Lesson 6, Step 4: one task per concern.
- `retention_interval` — `RETENTION_INTERVAL`, default 600 s. `partitions_ahead` — `PARTITIONS_AHEAD`, default 2 months. `disk_high_water` — `DISK_HIGH_WATER`, default 0.95. `disk_full_policy` — `DISK_FULL_POLICY`, default `degrade_retention`. Lesson 8, Step 3.
- `console_host` / `console_port` — `CONSOLE_HOST` (`127.0.0.1`) / `CONSOLE_PORT` (8080). `session_ttl` — `SESSION_TTL`, default 43200 s (12 h) for `console/auth.py::Sessions`.

### `__post_init__(self)`
Raises `ValueError` unless `disk_full_policy` is one of `stop_recording | degrade_retention | by_priority`; `tests/test_diskfull.py::test_bad_policy_is_rejected_at_startup` proves it.

## Notes
- Numeric parsing (`int(...)`, `float(...)`) is unguarded: a non-numeric value raises at startup with Python's own message, which is acceptable for a file provisioned by hand.
