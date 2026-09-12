# clustervms-go — М11, whole, in Go, measured against the Python original

М9 Lesson 9 argued that the rewrite touches only the actuator and proved it on one file. This directory is the same claim on the 2c shape of a whole cluster: every mechanism М10 and М11 designed — identity by claim, the epoch by check-and-set, the lease that fences the zombie, capacity as the worker's word, placement under label constraints with the server in the reason, the resource job with its peer mirror, the event index that is a cache, the snapshot that leaves the cluster — ported to Go **with the Python suite's 29 tests ported alongside, unchanged in meaning**. It is built **on** [`vmsserver-go`](../../М10_ServerVMS/vmsserver-go/README.md), М10's port, exactly as `clustervms/` is built on `vmsserver/`.

```
clustervms-go/
  go.mod                       module clustervms; requires vmsserver from ../../М10_ServerVMS/vmsserver-go (replace, not copy)
  cluster/
    variables.go               NomadVariables (net/http, the task's own token, cas, delete) and FakeVariables (one raft in memory, ACL)
    objectstore.go  s3.go      VariablesObjectStore (this cluster's choice: objects as Variables), HTTP, a directory, S3 with SigV4 + List
    worker.go                  ClusterWorker: the slot from NOMAD_ALLOC_INDEX, labels from NOMAD_META_labels, previous_hb for the RTO
    controller.go              ClusterController: Eligible / Place with labels and the server in the reason, Unplaceable, Snapshot, FailoverSeconds
    directory.go               where is camera 7 — one scan of vms/workers/*, cached by time; "w-1+w-2" during a move
    resource.go                VmsRoutes (/manifest, /segment with Range) on the platform's resource server; ClusterResource registers the VMS hook
    timeline.go                MergedTimeline across resources; unreachable named; "not lost"
    console.go                 /cameras /where /timeline /resources /unplaceable /events /metrics; POST /cameras, /marks; PUT /cameras
    *_test.go                  29 tests; bench_test.go — five operations timed
  cmd/clustervms/main.go       worker | controller | resource | eventindex — the four jobs, the same environment as the Python ones
  cmd/baseline/main.go         a server at idle: one worker and one controller with fifty cameras; prints its PSS
  cmd/pybaseline/baseline.py   the same shape in Python
  cmd/pybench/bench.py         the five operations in Python
  measure.sh                   runs all of it and prints the tables below
```

```bash
go test ./cluster/                      # 29 tests, ~150 ms
go test -race ./cluster/                # the epoch race with four real goroutines, race-detector clean
CLUSTERVMS_PATH=../clustervms ./measure.sh
```

## What was ported, and how

The Python file and the Go file have the same name and the same decisions; the shape around them is the one [`vmsserver-go`](../../М10_ServerVMS/vmsserver-go/README.md) describes (structs for rows, embedding for the layers, errors for exceptions, a slice for SQLite). Two things are specific to this module:

- **`replace vmsserver => ../../М10_ServerVMS/vmsserver-go`** in `go.mod` is the Go spelling of `sys.path.append(…/М10_ServerVMS/vmsserver)`: М10 is imported, not vendored, and a change there is a change here.
- **`OpenStore("variables://objects")`** is the default, as in Python: on a cluster of this size the heartbeats and the snapshot are Variables, and MinIO is not installed. `s3.go` keeps the SigV4 adapter (with `List` added for the heartbeat prefix) for a rented cluster or one that outgrows raft.

Not here: the GStreamer actuator (`cmd/clustervms worker` runs the fake and records nothing) and the Nomad job files, which are the Python package's and unchanged — a Go binary is what `Containerfile` copies in instead of an interpreter.

## The numbers (measured, Linux x86-64, Go 1.24.7, Python 3.11)

Both processes are **a server at idle**: one worker with fifty cameras placed on it (reconcile, pump, lease, heartbeat) and one controller (placement pass, redistribution, the snapshot), against the in-memory raft fake and a directory for the object store. PSS from `/proc/self/smaps_rollup`, three runs each.

| | Go | Python | |
|---|---|---|---|
| **worker + controller at idle, 50 cameras** | **8.9 MB** | **21.2 MB** | 2.4× — smaller than М9's 4× because the 2c worker is smaller than the old Node in both languages: no Postgres client, no publish/restore, no re-index sweep |
| Deployable artifact | one static binary, **6.8 MB** (arm64: 6.4 MB, one `GOARCH=arm64` away) | interpreter + the two packages, as М9 counted them | |
| Test suite | 29 tests in **150 ms** (570 ms with `go test`'s compile); М10's 41 in 140 ms | 29 tests in ~1.3 s, 42 in ~1.0 s (`run.py`'s own startup included) | Both are sub-second suites; the argument was never test speed. |
| Lines, non-test (М10 + М11) | ~5,600 | ~3,200 | 1.75× — error returns and types. Tests: 2,600 vs 1,400. |

The controller's actual work, per operation (`go test -bench` / `cmd/pybench/bench.py`, same inputs):

| Operation | Go | Python | |
|---|---|---|---|
| Issue an epoch by CAS (in-memory raft) | 0.82 µs | 1.9 µs | 2.3× |
| Directory scan, 1,000 workers × 20 cameras, then *where is 7007* | 2.3 ms | 2.6 ms | 1.1× — both are a thousand map copies |
| Place 120 cameras on 4 workers with labels | 89 ms | 170 ms | 1.9× — and both are dominated by the design, not the language: every `Place` re-reads the heartbeat objects from disk for `CapacityOf`, `LabelsOf` and `ServerOf`. Cache `WorkersSeen` for the pass and both drop by an order of magnitude (М11 Lesson 5, exercise 3) |
| Parse one bucket path (regexp + timestamp) | 0.85 µs | 11.7 µs | 14× — the only place Go is an order of magnitude ahead, and the sweep is the only place the resource touches a hundred thousand of anything |
| Encode + decode a 50-camera heartbeat | 226 µs | 130 µs | **0.6× — Python wins.** `encoding/json` over `map[string]any` reflects on every value; Python's `json` is C over a dict. A typed `Status` struct would reverse it, at the cost of the platform knowing the subsystem's status shape — which it must not |

## What the numbers say

**The win is memory and deployment, not speed** — which is what М9 Lesson 9 predicted and М11's first port measured. A cluster controller's work is reading a Variable, comparing two maps and deciding; Python does that within 2× of Go, and where the hot part is C underneath (JSON, regex) the gap is small or inverted. What Python cannot shed is the 12 MB it costs to be Python, and the interpreter-plus-wheels rootfs the appliance bundle has to carry. On a server whose worker count comes from `B + n·I`, a smaller `B` is more workers per server; on an appliance updated by a RAUC bundle, the four jobs become one 7 MB file with no interpreter to ship, cross-built for arm64 in one command.

**The design ported without a redesign, twice.** The first port moved a Node between servers; this one moves cameras between workers on a controller's rows — a different shape — and again twenty-nine tests written against Python objects pass against Go structs with only the syntax changed. That is the property to want: the risky part was the decisions, and the decisions survived a language *and* a redesign.

**Where Go made the code better, not just smaller:** the epoch race runs four goroutines in parallel and passes under `-race`; every "the store is unreachable, keep going" is a visible `err` instead of a bare `except`; the platform's *no camera here* rule is a test that greps the package. **Where it made it worse:** 1.75× the lines, and a heartbeat that is slower to encode because the platform refuses to know its shape.

**What this does not settle** is the media worker, and it does not try to: the per-frame rule from М9 Lesson 7 holds in Go exactly as it holds in Python, and the worker's pipeline is C either way.
