# Recorder — the М9 project, whole

The code Lessons 5–9 build, assembled as one runnable recorder. `INSERT INTO
cameras` starts a recording; `DELETE` stops it; `SIGKILL` loses the open
segment and nothing else; an operator sees all of it behind a login.

```
recorder/
  migrations/          Lesson 5 — schema, partitions, operators; Lesson 9 — conditions + camera_status
  worker/
    reconciler.py      Lesson 6 — the loop, with nothing in it (pure; survives the rewrite)
    pipeline.py        Lesson 7 — CameraPipeline state machine, GstActuator, FakeActuator
    retention.py       Lesson 8 — partitions ahead, detach+drop, the disk-full policy, orphan sweep
    worker.py         Lessons 6–9 — reconcile / pump_buses / report / retention, one process
    store.py           every SQL statement, in one file, so the column split is enforced in one place
    secrets.py         Lesson 5 — the credential that was hiding in rtsp_url
    config.py          settings from the environment (М8 Lesson 6)
  console/
    app.py             Lesson 9 — /login, /cameras, /status, /timeline, /events, /metrics
    auth.py            Lesson 9 — argon2, one account, marked temporary
  tests/               Lesson 8, Step 6 — converge, offline, stall, diskfull, restart (+ worker)
  tools/
    provision.py       key, operator, camera, migrate — the hand-provisioned things Lesson 5 counts
    fake_camera.py     an RTSP camera you can stall with a signal, socket held open
  quadlet/             Lesson 5, Step 9 — postgres.container, worker.container, env example
  Containerfile
```

## What maps to what

| Lesson says | Where it is |
|---|---|
| The recorder owns this database; nothing above writes these rows | `store.py` — `report()` and `set_condition()` are the only writers of controller-owned columns; `create_camera()` / `update_camera()` accept operator-owned columns only |
| `revision` is an integer; `observed_revision >= revision` is "applied" | `migrations/0001` trigger; `reconciler.py` line with `>=` |
| The credential hiding in `rtsp_url` | `secrets.py` (AES-GCM, camera id as associated data); `console/app.py` refuses a URL with `user:pass@`; `pipeline.py` composes the URL in memory and `del`s it |
| Partition by time from day one; `DELETE` is not retention | `migrations/0002` (`PARTITION BY RANGE (lower(span))`), `retention.py` (`DETACH` then `DROP TABLE`, paths remembered first) |
| The pruning trap | `store.timeline()` and the `camera_status` view both bound `lower(span)`, with the comment the lesson asks for |
| Desired persisted, actual derived; `self.actual = {}` IN MEMORY ONLY | `reconciler.py` — and `tests/test_converge.py::test_5` keeps the lying `Persisted` class as a test, not a class |
| The timer is correctness, the notification is latency | `worker.reconcile()` waits on `wake` **or** `poll_interval`; `store.listen()` sets `wake` |
| One task per concern, not per camera | `worker.py` — four tasks, whatever the camera count |
| Backoff with jitter | `reconciler._fail()`; `tests/test_offline.py` proves a 0.000 s spread without it |
| Python touches control, never data | `pipeline.py` — no probes, no `appsink`; `watchdog` in the description; `format-location` once per segment; non-blocking `pop_filtered` |
| The spool becomes the archive | `pipeline._closed()` → `Worker._on_segment_closed()` queues → `report_once()` writes the index row |
| `epoch` in the path, 1 and unused | `pipeline.segment_dir()` → `/data/archive/<cam>/e1/` |
| Drop the index before unlinking the files | `retention.py`, and `tests/test_diskfull.py` asserts the order from the operation log |
| The recorder says which policy it applied | `retention.degraded` / `retention.stopped` events in the `events` table |
| On restart, never resume the previous segment | segment names are the wall-clock start; `tests/test_restart.py` |
| Positions and reasons on separate axes | `phase` column vs `camera_conditions` table; `worker.phases()` never sets a phase from a condition |
| One query | `camera_status` view, `migrations/0004` |
| Two exported signals; `camera_lag` as a distribution | `console.render_metrics()` |
| Same 401 for unknown user and wrong password | `console/app.py::login` |

## Running it on the bench

```bash
# 1. Postgres on the data partition (Lesson 5, Step 9)
mkdir -p /data/pg /data/config /data/archive
podman run -d --name pg -e POSTGRES_USER=recorder -e POSTGRES_PASSWORD=change-me \
  -e POSTGRES_DB=recorder -v /data/pg:/var/lib/postgresql/data:z -p 127.0.0.1:5432:5432 postgres:16

# 2. dependencies (GStreamer from the OS, the rest from pip)
sudo apt install python3-gi gstreamer1.0-plugins-{base,good,bad} gir1.2-gst-plugins-base-1.0 \
                 gir1.2-gst-rtsp-server-1.0
pip install -e '.[test]'
gst-inspect-1.0 watchdog          # must print an element

# 3. commission (the temporary secrets, counted)
export DATABASE_URL=postgresql://recorder:change-me@127.0.0.1:5432/recorder
export ARCHIVE_DIR=/data/archive COLUMN_KEY_FILE=/data/config/column.key
python3 -m tools.provision migrate
python3 -m tools.provision key
python3 -m tools.provision operator admin

# 4. a camera you can unplug
python3 tools/fake_camera.py --port 8554 --count 50 &

# 5. run the recorder
python3 -m worker
```

Then, in another shell:

```bash
TOKEN=$(curl -s -XPOST localhost:8080/login -H 'content-type: application/json' \
        -d '{"username":"admin","password":"..."}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -XPOST localhost:8080/cameras -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
     -d '{"name":"lobby","rtsp_url":"rtsp://127.0.0.1:8554/cam0","site_id":"hq"}'
curl -s localhost:8080/status -H "authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s localhost:8080/metrics
kill -USR1 $(pgrep -f fake_camera)     # stall every stream, sockets open: watch the watchdog fire
kill -USR2 $(pgrep -f fake_camera)
```

`INSERT INTO cameras` from `psql` works just as well — the console is one client of the store, not the only one.

## Tests

```bash
pytest                    # the millisecond suite; test_stall.py skips without GStreamer
python3 tests/run.py      # the same suite without pytest, one line per test
NODEVMS_STALL_TEST=1 pytest tests/test_stall.py     # needs GStreamer + gst-rtsp-server
```

## What was verified where

Honest accounting, in the module's own convention:

- **Run, output real:** `tests/` — 27 tests, plain Python, no database, no GStreamer (reconciler, backoff and jitter, retention order and all three disk-full policies, the Worker glue, segment naming, the credential encryption and the URL validator). Every SQL statement in `store.py` and all four migrations were executed against **PostgreSQL 16.13**, twice (idempotency), including `DETACH`/`DROP`, the `camera_status` view and the `ON CONFLICT` upsert that keeps `since` still.
- **Written to the documentation, not executed here:** the GStreamer path in `pipeline.py` (`splitmuxsink-fragment-closed` element messages, `format-location`, `pop_filtered`), `tools/fake_camera.py`, and the HTTP layer of `console/app.py`. They compile and import; they need a bench with GStreamer 1.18+ and a real `asyncpg`/`fastapi` install to run, which the authoring sandbox did not have. Run `tests/test_stall.py` first when you have one.

## One correction to Lesson 5, found by running it

The lesson's trigger is

```sql
CREATE TRIGGER cameras_bump BEFORE UPDATE ON cameras
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*)
    EXECUTE FUNCTION bump_revision();
```

Whole-row comparison means the Worker's own `report()` — writing
`observed_revision`, `phase`, `last_seen` — bumps `revision`. Measured:
report `observed_revision = 2` and `revision` goes to 3; the lag is 1
forever and the recorder chases its own tail. `migrations/0001` names the
operator-owned columns in the `WHEN` clause instead, which is the column
split from Step 3 enforced by the database rather than by convention.

## Known gaps, named

- The column key is a file on the data partition (Lesson 5 says so; М11 delivers it as a Nomad Variable).
- `stop_recording` stops *new* pipelines and lets running ones finish their open segment; it does not pre-empt a segment mid-write.
- The orphan sweep reports and never deletes (Lesson 8, exercise 4 — the decision is yours).
- `fake_camera.py` stalls every stream on `USR1`; a per-stream stall is a small change to the valve wiring and a good exercise.
- `CameraPipeline.stop()` sends EOS and goes to NULL without waiting for the muxer to finalize; a graceful drain is a `timed_pop_filtered` for EOS, which blocks, so it belongs in shutdown only. `SIGTERM` therefore behaves like the lesson's `SIGKILL` for the open segment.
- Sessions are in memory. This is the fourth temporary secret; М12 Lesson 4 replaces it.
