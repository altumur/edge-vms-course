# failover-drill.sh — Lesson 4, Step 6: the power pull, measured. Three runs, the worst case kept

**Role.** The script that produces the datasheet number for failover on a real cluster. `deploy/failover-drill.sh w-1 <console host:port> [runs]` — for each run it finds the server running the given worker slot, pulls that server (a node drain with a zero deadline stands in for the power cut; the header says to use М9's `bench/outage.sh power-cut` for the real thing), waits for the worker to heartbeat from *another* server, reads `vms_failover_seconds` from the console's `/metrics`, returns the server, and after its old instance has woken reads `vms_epoch_conflicts` — the proof that the old instance fenced rather than wrote. Needs `nomad` (with a management `NOMAD_TOKEN`), `curl`, `python3`, `awk`. `set -u` only: a failed command does not abort, but two explicit checks `exit 1`. Exit 0 after the summary line otherwise.

## Environment / arguments
- `$1` `WORKER` — the slot, e.g. `w-1` (required).
- `$2` `CONSOLE` — a console `host:port`, e.g. `10.0.0.11:8080` (required).
- `$3` `RUNS` — number of runs, default 3.
- `INDEX` — `WORKER` with the `w-` prefix stripped: the `NOMAD_ALLOC_INDEX` to look for.
- `NOMAD_ADDR`/`NOMAD_TOKEN` — read by the `nomad` CLI, not by the script.

## Step by step

### `alloc_node()`
Reads `nomad job allocs -json vmsworker` from stdin and prints the `NodeID` of the running allocation whose `Index` equals `$1`, excluding NodeID `$2` (passed as `""` here, so no exclusion). This is how "the server running `w-1`" is found: the allocation index is the slot preference, so the running allocation with that index is the slot's holder.

### `row_server()`
Reads the console's `/cameras` JSON and prints the `server` field of the first row whose `worker` is `$1` and `worker_state` is `live`; empty until the worker heartbeats again (or if the body is not JSON — the console may be the one that was drained). The rows carry the server because the heartbeat does.

### the run loop
- `old` = the NodeID of the running allocation with `INDEX`; abort with exit 1 if none.
- `before` = the server the console currently shows for the worker; `t0` = wall-clock seconds.
- `nomad node drain -enable -deadline 0s -yes "$old"` — the pull: every allocation on the node is stopped immediately, with no migration grace, which is the closest an orderly API gets to a power cut.
- Poll `/cameras` once a second for up to 180 s until the worker's row shows a live `server` different from `before`; abort with exit 1 on timeout. `t1` = the time of the first different answer.
- `secs` = the first `vms_failover_seconds` value in `/metrics` — the worker's own measurement (last heartbeat of the old instance → the new instance's start), which is the RTO the tests fix at 48 s on the fake clock.
- Print the wall-clock `t1 - t0` (drain + `lost_after` + schedule + claim + first heartbeat + the console's own poll) beside it; `worst` = the max wall-clock over runs so far.
- `nomad node drain -disable -yes "$old"` — return the server; `sleep 30` so its old instance (if it was only paused/partitioned) has had a lease pass.
- `conflicts` = `vms_epoch_conflicts{worker="w-1"}` from `/metrics` — the old instance's CAS conflicts on its epochs: non-zero means it found its epochs taken and fenced, its footage kept under its old epoch (Lesson 4's "the old instance wakes up").

### the summary
`failover worst case over N runs: <worst>s` — the datasheet number; the console's `vms_failover_seconds` is noted as the worker's own measurement.

## Notes
- The `awk -v w="$WORKER"` for `vms_failover_seconds` never uses `w`, and `$0 ~ "vms_failover_seconds"` matches the `# TYPE vms_failover_seconds gauge` comment line first, so `$2` prints the word `TYPE`. Even skipping the comment, `SpecConsole.metrics_text` emits only `vms_failover_seconds{kind="worst"}` (0.0 when `__main__.console` passes no `worst_failover`) — there is no per-worker failover gauge in `/metrics` to read. The wall-clock `t1 - t0` is the number the drill actually measures. The conflicts query does filter by worker and skips its TYPE line because the pattern includes `{worker="`.
- A drain also stops the console and the resource on that server if they run there; the script assumes `CONSOLE` points at a *different* server than the one holding the worker.
- `worst` is compared as Python numbers via `python3 -c`, so it is an integer number of seconds.
