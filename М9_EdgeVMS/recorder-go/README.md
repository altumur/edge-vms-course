# recorder-go — the rewrite that touches only the actuator

М9 Lesson 9 argues that Python was the right language to *design* the recorder
in and the wrong one to *ship* the controller in, and that the rewrite
touches only the actuator: the schema, the loop's logic, the state machine,
the backoff and jitter policy and the desired/actual contract survive
unchanged. This directory is the smallest proof of that claim.

```
recorder-go/
  reconciler/reconciler.go        Lesson 6's loop, in Go — every line a decision, the same decisions
  reconciler/reconciler_test.go   the Python suite's seven tests plus the cap test, line for line
  cmd/baseline/main.go            a controller-shaped Go process at idle; prints its own PSS
  cmd/pybaseline/baseline.py      the same shape in Python (recorder' reconciler, asyncio, an HTTP listener)
  measure.sh                      runs both and prints the numbers
```

```bash
go test ./reconciler/ -v          # 8 tests
./measure.sh                      # the comparison below
```

## The port

`reconciler.go` is `worker/reconciler.py` with types. Read them side by side: the same `>=` on the revision, the same stop loop over *actual* rather than *desired*, the same `base * (0.5 + rand*0.5)` jitter, the same `lost()` for a pipeline that died, the same `converged | lagging | stalled` vocabulary with reasons kept off it. `SetActual` exists only so Test 5 can make the persisted-actual mistake on purpose, exactly as the Python `Persisted` subclass does.

The tests are the point. They were written against the Python loop, they pass against the Go loop without a change of meaning, and nothing in them mentions a language — which is what *the risky part was the design, not the code* looks like when it is true.

## The numbers (measured, Linux x86-64, Go 1.24.7, Python 3.11)

Both processes hold a reconciler with fifty converged cameras, a status map, a JSON encoder and an HTTP listener, at idle. No GStreamer in either — GStreamer costs the same in every language (24 MB of libraries here) and is measured separately by М9's `shard-memory-probe.py`. PSS, not RSS, from `/proc/self/smaps_rollup`.

| | Go | Python | |
|---|---|---|---|
| Controller at idle, 50 cameras | **6.0 MB** | **25.7 MB** | 4.3× — this is the controller's share of Lesson 7's `B` |
| Bare runtime (`main(){}` / `python3 -c pass`) | 1.9 MB | 6.2 MB | |
| Deployable artifact | one static binary, **5.5 MB** | interpreter 55 MB + PyGObject 2 MB + pydantic, cryptography, starlette, uvicorn, anyio ≈ 28 MB (asyncpg, fastapi, argon2 not counted — not installed on the measuring machine) | the part of М9's RAUC bundle that the language decides |
| The ARM appliance (М9 appendix) | `GOOS=linux GOARCH=arm64 go build` — 5.1 MB, no toolchain on the box | a cross-built rootfs with the interpreter and every wheel | |
| Start to first reconcile | milliseconds | interpreter start + imports | the head of `node_failover_seconds` |

What the table does **not** say: that the media worker should be Go. Lesson 7's per-frame rule survives — a per-buffer callback in Go crosses cgo from a C thread — so the worker stays in C++ (or Python, until on-box analytics ends that) and the controller is where Go wins. It also does not say Python was the wrong choice for the course: the eight tests above were cheap to get right in Python and free to port.

## What a full port would carry over

| Survives as-is | Ported mechanically | Rewritten |
|---|---|---|
| the schema and migrations (SQL) | `worker.py`'s four tasks → goroutines and a ticker; `store.py` → `pgx`; `retention.py`; `console/app.py` → `net/http` | the actuator (`pipeline.py`) — and it becomes a **client** of a C++ worker, not a host of pipelines |
| the tests' meaning | `vmsserver/` and `clustervms/` — done: [`vmsserver-go/`](../../М10_ServerVMS/vmsserver-go/README.md) and [`clustervms-go/`](../../М11_ClusterVMS/clustervms-go/README.md) port the whole of М10 and М11 with their 70 tests, in the standard library (`net/http` to Nomad rather than `github.com/hashicorp/nomad/api`, which is MPL-2.0), and measure a server's worker and controller in both languages | |
