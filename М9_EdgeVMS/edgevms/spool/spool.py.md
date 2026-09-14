# spool.py — the spool, its uploader, and the two numbers

**Role in the module.** Lesson 4, Steps 6 and 9. The bounded on-disk queue between capture and the cloud: `splitmuxsink` closes segments into `/data/spool/<camera>/<timestamp>.mp4`, and this file, running as a separate process (`quadlet/spool-uploader.container`), pushes them to KVS oldest-first and deletes each one only when the far side has acknowledged it. Four properties, each a decision: delete on acknowledgement, never on send; a bound, and a policy for reaching it that the appliance SAYS it applied; oldest first, stop at the first failure; rate-limited catch-up. It also writes two numbers (`spool_oldest_seconds`, `spool_bytes_used`) to a file the health check can read with the uplink down. Plain standard library; tested by `spool/test_spool.py` (six tests, run for real per the README). М9's recorder later turns this spool into an archive by keeping an index of it; М13 gives the numbers an exporter. The upload command receives the segment path as its last argument and its exit status is the acknowledgement.

## `class Spool`
A directory with a byte bound and a policy. State: `root`, `max_bytes`, `policy` (`drop-oldest` or `stop-recording`), and `dropped`, a counter of segments sacrificed to the bound. Nothing else is remembered between calls — the directory listing is the queue.

### `__init__(self, root, max_bytes, policy="drop-oldest")`
Stores the three parameters, creates `root` if needed, zeroes `dropped`.

### `pending(self)`
All `*.mp4` directly under `root` and one level down (per-camera subdirectories), sorted by basename. The filename carries the timestamp, so sorting is ordering — across cameras too, which is what test 5 proves.

### `used(self)`
Sum of the sizes of everything pending. Recomputed from disk each time; correct after a crash, and cheap at spool sizes.

### `accept(self, name, data)`
Called when a segment closes; returns whether it was kept. If adding `data` would exceed `max_bytes`: under `stop-recording` return `False` without writing (the capture side must then stop); under `drop-oldest` remove pending segments from the oldest until it fits, incrementing `dropped`. Then write `root/name` and return `True`. Writes go into `root` itself, not a camera subdirectory.

### `oldest_seconds(self, now=None)`
Age of the oldest pending segment by mtime, or 0.0 if the spool is empty. `now` is injectable for tests.

### `signals(self, now=None)`
The two numbers (Step 9) plus two context values, as a dict in metric-name form: `spool_oldest_seconds` (rounded to 0.1), `spool_bytes_used`, `spool_bytes_bound`, `spool_segments_dropped_total`. Age of the oldest unsent segment is the alarm; bytes used over bound is the fullness.

## Functions
### `drain(spool, upload, budget_per_tick=2)`
One pass of rate-limited catch-up: walk `pending()` oldest first, call `upload(path)`, and `os.remove` the file only on a truthy return — delete ON ACKNOWLEDGEMENT, never on send. Stop at the first failure so order is preserved, and never send more than `budget_per_tick` in one pass. Returns the number sent.

### `write_signals(path, signals)`
Writes `name value` lines (Prometheus text format) to `path + ".tmp"` and `os.replace`s it into place, so a reader never sees a half-written file. Creates the parent directory.

### `main() -> int`
The uploader process. Arguments: `--root` (default `/data/spool`), `--max-bytes` (required — "Lesson 1's number: cameras × bitrate × outage"), `--policy`, `--budget` (default 2), `--tick` (default 5.0 s), `--upload-cmd` (required; the path is appended; exit 0 = acknowledged), `--signals` (default `/run/vms/spool-signals`). Builds a `Spool`, defines `upload(path)` as a `subprocess.run` of the command with the path appended (stderr excerpt printed when not acknowledged), then loops forever: `drain`, compute and write signals, print "spool full: policy=… dropped=N" whenever `dropped` advanced since the last tick (the appliance says which policy did what), print a drain summary when anything was sent, sleep one tick. Never returns.

## Notes
- `accept()` is the capture-side hook; on the real box the agent's `splitmuxsink` writes files itself and only `drain`/`main` run here, which is why the uploader is a separate process: somebody other than the writer does the deleting.
- `used()` calls `pending()` and the eviction loop calls both per iteration — quadratic in the number of segments, irrelevant at a few thousand files.
