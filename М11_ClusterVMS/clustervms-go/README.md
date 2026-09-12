# clustervms-go — М11, whole, in Go, measured against the Python original

М9 Lesson 9 argued that the rewrite touches only the actuator and proved it on one file. This directory is the same claim on a whole module: every mechanism М11 designed — identity from a Variable, the epoch by check-and-set, publish-then-point, the six-step restore, the lease that fences the zombie, the heartbeat as an object, the re-index sweep, the directory, placement, SigV4 — ported to Go **with the Python suite's 29 tests ported alongside, unchanged in meaning**, plus one test the Python version could not have: a Go recorder restoring what a Python recorder published.

```
clustervms-go/
  go.mod                       module clustervms; imports recorder/reconciler from ../recorder-go (not copied)
  cluster/
    variables.go               Variables: NomadVariables (net/http, the task's own token) and FakeVariables (raft in memory, CAS, ACL)
    epoch.go                   NextEpoch by CAS; Lease (renew = read my epoch; may_write on a monotonic clock)
    publish.go                 object first, then the Variable; the floor; the `replicated` condition
    rehydrate.go               the six steps; unconfigured invents nothing; a dangling pointer is refused
    identity.go                who am I — from the template's environment, then the Variable
    configio.go                the format-1 blob, byte-compatible with Python's
    directory.go               the scan, cached by time; "where is camera 7"; two answers is an error
    placement.go               measured capacity, labels, stored placement, budgeted rebalance, the invariants
    reindex.go                 files back into rows, the epoch kept from the path
    objectstore.go  s3.go      fs · http · S3 with SigV4 (Amazon's worked examples reproduced)
    apphost.go                 ClusterAppHost: the prologue, the gate, one goroutine per concern
    metrics.go  console.go     node_failover_seconds, node_epoch_conflicts, …; /cluster/node, /cluster/directory, /cluster/where/N
    store.go                   the Store interface and FakeClusterStore
    pgstore.go                 the Postgres Store on pgx — build tag `pg` (see below)
    settings.go  actuator.go   the environment; the Actuator interface and the fake
    *_test.go                  30 tests; bench_test.go — six operations timed
  cmd/clustervms/main.go       the recorder: NOMAD_ADDR, NODE_ID, OBJECT_STORE_URL, … — the same environment as the Python one
  cmd/baseline/main.go         the whole host at idle with fifty cameras; prints its PSS
  cmd/pybaseline/baseline.py   the same shape in Python
  cmd/pybench/bench.py         the six operations in Python
  measure.sh                   runs all of it and prints the tables below
```

```bash
go test ./cluster/                      # 30 tests, ~100 ms
go test -race ./cluster/                # the CAS race with four real goroutines, race-detector clean
RECORDER_PATH=../../М9_EdgeVMS/recorder CLUSTERVMS_PATH=../clustervms ./measure.sh
```

## What was ported, and how

Read each Go file beside its Python twin: the names are the same and so are the decisions. `>=` on the revision, the object before the pointer, `TTL − margin` on a monotonic clock, `free > best_free` with a sorted tiebreak, the two-segment grace before a file is re-indexed, the `e<epoch>` directory as the fencing token — none of it changed. What changed is the shape around it:

| Python | Go | Why it matters |
|---|---|---|
| `asyncio` tasks, one per concern | goroutines, one per concern, a ticker each, `context` to stop them | The same process model. The `wake` event became a 1-slot channel. |
| `threading.Thread × 4` racing `next_epoch` | four goroutines, really parallel, `-race` clean | The Python test proves CAS under the GIL; the Go test proves it under real concurrency. |
| exceptions (`Conflict`, `Forbidden`, `RestoreRefused`) | `ErrConflict`, `ErrForbidden`, `*RestoreRefused` with `errors.Is`/`As` | Every `try/except Exception: log` in the Python became an explicit `if err != nil` — about half the extra lines. |
| `dict` Variables items, values stringified on `put` | `Items map[string]string` | Nomad stores strings; the Go type says so. |
| `dataclass` config blob, `sort_keys=True` | struct tags; `[]byte` → base64 automatically | **Byte-compatible**: `TestAGoNodeRestoresWhatAPythonNodePublished` restores a blob captured from the Python fake; the reverse was run by hand (a Python `rehydrate()` over Go's object: `restored, revision 4, 2 cameras`). |
| `ClusterStoreMixin` + М9's `PgStore` on asyncpg | `Store` interface; `PgStore` on pgx behind `-tags pg` | The measuring machine could not reach the Go module proxy, so `pgstore.go` was **type-checked against a stub of pgx's signatures, not run against Postgres**. The SQL is the Python version's, verified in М9 on Postgres 16. `go get github.com/jackc/pgx/v5 && go build -tags pg ./cmd/clustervms` is the missing step; without the tag the binary builds and refuses at start. |
| `urllib.request` to Nomad | `net/http` to Nomad | Still no `github.com/hashicorp/nomad/api` (MPL-2.0 — the note from М9 Lesson 9 stands): the four calls the recorder makes are forty lines. |

Two things are not here, and were not in the Python `clustervms/` either: the GStreamer actuator (М9's, and in a Go controller it becomes a *client* of a C++ media worker — Lesson 5's per-frame rule survives cgo) and М9's retention task and console. `cmd/clustervms` runs the recorder with the fake actuator: everything about identity, restore, epoch, lease, directory and metrics is real; nothing records.

## The numbers (measured, Linux x86-64, Go 1.24.7, Python 3.11)

Both processes are the **whole recorder at idle**: fifty cameras restored from the directory, epoch taken, every task running (reconcile, report, publish, lease, heartbeat, re-index; in Python also М9's bus pump and retention), a console listener, fakes for Nomad and Postgres, a directory for the object store. PSS from `/proc/self/smaps_rollup`, three runs each.

| | Go | Python | |
|---|---|---|---|
| **recorder at idle, 50 cameras, every task running** | **7.1 MB** (10 goroutines) | **28.5 MB** (11 asyncio tasks) | 4.0× — the same ratio М9 measured on the reconciler alone (6.0 vs 25.7). The cluster layer added 1 MB in Go and 3 MB in Python. |
| Deployable artifact | one static binary, **6.6 MB** (arm64: 6.2 MB, one `GOARCH=arm64` away) — without pgx | interpreter + asyncpg + the rest, as М9 counted them | |
| Test suite | 30 tests in **96 ms** (755 ms with `go test`'s compile) | 29 tests in **241 ms** | Both are millisecond suites; the argument was never test speed. |
| Lines, non-test | 2,640 (of which 320 are `pgstore.go`, and settings/actuator/fake store that live in `recorder/` on the Python side) | 1,250 | About twice — error returns and types. Tests: 920 vs 600. |

The controller's actual work, per operation (`go test -bench` / `cmd/pybench/bench.py`, same inputs):

| Operation | Go | Python | |
|---|---|---|---|
| SigV4 sign of a 64 kB object | 56 µs | 69 µs | **1.2×** — it is SHA-256 in both, and both are C underneath. The language does not own the hash. |
| Issue an epoch by CAS (in-memory raft) | 0.84 µs | 1.75 µs | 2.1× |
| Directory scan, 1,000 recorders × 20 cameras, then *where is 70007* | 5.3 ms | 8.0 ms | 1.5× — both are a thousand map copies; neither is the cost of a console page (raft round-trips are). |
| Place 120 cameras on 4 recorders with labels | 0.92 ms | 1.79 ms | 1.9× |
| Parse one segment path (regex + timestamp) | 0.77 µs | 12.3 µs | 16× — the only place Go is an order of magnitude ahead, and the sweep is the only place the controller touches a hundred thousand of anything. |
| Encode + decode a 200-camera configuration | 0.57 ms | 0.72 ms | 1.3× |

## What the numbers say

**The win is memory and deployment, not speed** — which is what М9 Lesson 9 predicted. A cluster controller's work is reading a Variable, hashing an object, and comparing two maps; Python does that within 2× of Go, and hashing within 20 %, because the hot part of each is already C. What Python cannot shed is the 20 MB it costs to be Python, and the interpreter-plus-wheels rootfs that М9's bundle has to carry. On a recorder whose budget is `B + n·I`, four times smaller `B` is more cameras per box; on an appliance whose update is a RAUC bundle, the controller's whole contribution to the bundle becomes one 6 MB file with no interpreter to ship, cross-built for arm64 in one command.

**The design ported without a redesign.** Thirty tests, written against Python objects, pass against Go structs with only the syntax changed, and a recorder in either language restores from the other's publication. That is the property to want from a design: the risky part was the decisions, and the decisions survived a language.

**Where Go made the code better, not just smaller:** the CAS race test runs four goroutines in parallel and passes under `-race`; the Python version proves the same thing under the GIL, which is a weaker proof. Every "the store is unreachable, keep going" became a visible `err` instead of a bare `except`. **Where it made it worse:** twice the lines, and a Postgres driver that had to be fetched — the standard library got the recorder to Nomad and S3 without a dependency, but not to Postgres.

**What this does not settle** is the media worker, and it does not try to: the per-frame rule from М9 Lesson 7 holds in Go exactly as it holds in Python, and the worker is C++ either way.
