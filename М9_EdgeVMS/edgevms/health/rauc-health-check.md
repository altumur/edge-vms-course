# rauc-health-check — decides whether the running slot is kept: exit 0 = mark good

**Role.** Lesson 3, Step 3 (course-wide "Lesson 18, Step 3" in the header). Run by `health/rauc-mark-good.service` as `ExecStart`; if it exits 0 the unit's `ExecStartPost` runs `rauc status mark-good` and the slot's `TRY` flag is cleared, otherwise nothing is marked and the next boot falls back to the other slot. It is the highest-stakes alert rule in the course and it runs ON THE BOX and touches NOTHING outside it: a check that asked a remote system would turn a network fault into a fleet-wide rollback. POSIX `sh`, `set -e`; needs `systemctl`, `curl`, `awk`, `find`, and `python3` for the М10 branch.

The ladder, bottom row the one that matters:
1. no failed systemd units (crashed services);
2. the VMS answers (startup failures);
3. footage is actually being written (the update that boots perfectly and records nothing).

Row 3 has three sources, tried in order: М10's VMS console on the box, М9's own recorder, and — the М9-only bench — the Lesson 3 stand-in of a segment appearing in the spool.

## Environment variables
- `VMS_CONSOLE` — М10 console base URL, default `http://127.0.0.1:8080`.
- `ARCHIVE` — М10 archive root, default `/data/archive`; segments are looked for under `$ARCHIVE/vms`.
- `NODE_METRICS` — the М9 recorder's metrics URL, default `http://127.0.0.1:8080/metrics` (`console/app.py`, `CONSOLE_PORT`).
- `AGENT_HEALTH` — the М8 agent's health URL, default `http://127.0.0.1:8000/health` (`quadlet/vms-agent.container` publishes 8000).
- `SPOOL` — default `/data/spool`.
- `SEGMENT_SECONDS` — default 600; "Lesson 22 (М9 Lesson 7) makes it a product decision; this follows it".
- `HEALTH_WINDOW` — override for `WINDOW`, default `2 × SEGMENT_SECONDS + 60` (1260 s): a healthy recorder is silent for up to one segment length between closes, so a 120 s window against ten-minute segments would roll back every good update.

## Step by step
### `fail()`
Prints `health: <reason>` to stderr and exits 1. Every refusal goes through it, so the journal of `rauc-mark-good.service` says why a slot was not confirmed.

### 1. No failed units
`systemctl --failed --no-legend` piped to `grep -q .`; if anything is listed it is printed and the check fails with "failed units present". This is the row that catches a service that crashed at boot regardless of which VMS is installed.

### 2 + 3, source one: М10's VMS console
`curl -fsS --max-time 5 "$VMS_CONSOLE/metrics"` must succeed *and* the body must contain a `vms_workers_live ` series — that series is how the script tells the М10 console apart from the М9 recorder console, which listens on the same port but exports `recorder_*` names. Inside the branch:
- `val()` — awk lookup of one series by exact name in `$METRICS`, printing `MISSING` if absent.
- `vms_workers_live` and `vms_cameras_running` must both exist, else "missing a required series".
- `live >= 1`, else "no worker is live": the console is up and nobody is home.
- `configured` — `GET $VMS_CONSOLE/cameras`, JSON, count of entries in `configured` whose `enabled` is not false (the М10 console's `/<rows>` reply is `{rows, configured}`; see `vmsserver/vms/console.py.md`). If curl or the JSON parse fails it is `MISSING` → fail "answered /metrics but not /cameras".
- `configured == 0` → pass and say so: no cameras, no footage to prove, rows 1–2 only. A stated product decision.
- `running >= 1`, else "N camera(s) configured, none held".
- `recordings` — `GET $VMS_CONSOLE/rec/recordings`, the recorder's rows through the console's mount (М10 Lesson 5); `0` → pass, "no recordings configured — footage test vacuous" (a camera may be watched and never recorded).
- `recording` — `rec_recordings_running` from `GET $VMS_CONSOLE/rec/metrics`; missing → fail; `>= 1`, else "N recording(s) configured, none running".
- the segment test reads `$ARCHIVE/rec` — the recorder's tree, `rec/<cam>/e<epoch>/` — not `vms/`, which holds the worker's event buckets.
- `find "$ARCHIVE/vms" -name '*.mp4' -newermt "-${WINDOW} seconds"` must find something: a segment promoted into the archive resource within the window, read from the disk on this box — "not a heartbeat that could be lying". Passes with a summary line.

### Source two: М9's own recorder
If `curl $NODE_METRICS` succeeds (and source one did not claim the port), the same `val()` reads four series from `console/app.py::render_metrics`: `recorder_node_reporting`, `recorder_cameras`, `recorder_camera_silent_seconds_max`, `recorder_cameras_never_recorded`. Any missing → fail. Then, in the order the README's table lists:
- `reporting = 1`, else "the Worker is not reporting" — the console answers but the Worker task loop has not written `last_seen` within 3 × `REPORT_INTERVAL`.
- `cameras == 0` → pass, "no cameras configured — footage test vacuous"; commissioning should add a camera before the first update.
- `never < cameras`, else "no camera has written a segment" — Lesson 3's failure two, the update that boots and records nothing.
- `${silent%.*} <= WINDOW` (integer part of the max silence), else "newest segment is Ns old".
- Otherwise pass with the camera count and silence.

### Source three: the М8 agent and the spool (М9-only bench)
`curl $AGENT_HEALTH` must answer, else fail naming both URLs that did not. Then the stand-in: a `*.mp4` under `$SPOOL` newer than the window. Weak on purpose — a fast uploader empties the spool — and replaced by the rows above whenever a recorder is present. Passes with "agent + spool stand-in".

## Notes
- Both consoles default to port 8080, so the script can only be right about which one answered because of the `vms_workers_live` grep; a recorder metrics page reaches the М9 branch by falling through, at the cost of a second `curl`.
- `recorder_cameras_never_recorded` is, per its HELP text, "enabled cameras with no segment in the last two hours" (the `camera_status` view bounds `lower(span) > now() - 2 hours` for partition pruning), and those cameras have `silent_for` NULL, so they are *excluded* from `recorder_camera_silent_seconds_max`. A camera silent for three hours therefore does not raise the silence gauge; the check only fails if *every* camera is in that state (`never == cameras`), and then with the message "no camera has written a segment" rather than "newest segment is old". The README's verified scenarios (silent 45 min; never recorded) both sit on the right side of the two-hour boundary.
- `rauc-mark-good.service` sleeps 150 s before running this; with the default `SEGMENT_SECONDS=600` a freshly booted recorder has not closed its first segment by then, so a new camera would count as never recorded. See the note on the service file.
- The README's table row "newest segment older than 2 × SEGMENT_SECONDS + 60" is the `silent` test; "Worker not reporting" is `reporting`; "cameras configured, none has ever written a segment" is `never`.
