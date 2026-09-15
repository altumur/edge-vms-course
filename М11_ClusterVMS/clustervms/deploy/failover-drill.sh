#!/usr/bin/env bash
# М11 Lesson 4, Step 6 — the power pull, measured. Three runs, worst case kept.
#
#   deploy/failover-drill.sh w-1 <console host:port> [runs]
#
# For each run: find the server running the worker, pull it (a drain with a
# zero deadline stands in for the power cut; use М9's bench/outage.sh
# power-cut for the real thing), wait for the worker to heartbeat from
# another server, read vms_failover_seconds from the console, return the
# server, and read vms_epoch_conflicts after its old instance has woken.
set -u
WORKER="${1:?worker slot, e.g. w-1}"; CONSOLE="${2:?console host:port, e.g. 10.0.0.11:8080}"; RUNS="${3:-3}"
INDEX="${WORKER#w-}"

alloc_node() {                # $1 = alloc index; $2 = a NodeID to exclude, or ""
  IDX="$1" EXCLUDE="$2" python3 -c '
import sys, json, os
a = [x for x in json.load(sys.stdin) if x["ClientStatus"] == "running" and str(x["Index"]) == os.environ["IDX"]
     and x["NodeID"] != os.environ["EXCLUDE"]]
print(a[0]["NodeID"] if a else "")'
}
row_server() {                # the worker's server from /cameras rows, "" until it heartbeats again
  W="$1" python3 -c '
import sys, json, os
try:
    rows = json.load(sys.stdin)["rows"]
except ValueError:
    rows = []
r = [x for x in rows if x["worker"] == os.environ["W"] and x["worker_state"] == "live"]
print(r[0]["server"] if r else "")'
}

worst=0
for i in $(seq 1 "$RUNS"); do
  old="$(nomad job allocs -json vmsworker | alloc_node "$INDEX" "")"
  [ -n "$old" ] || { echo "no running allocation with index $INDEX"; exit 1; }
  before="$(curl -s "http://$CONSOLE/cameras" | row_server "$WORKER")"
  t0=$(date +%s)
  echo "run $i: $WORKER on $before ($old); pulling at $(date -u +%H:%M:%S)"
  nomad node drain -enable -deadline 0s -yes "$old" >/dev/null

  after=""
  for _ in $(seq 1 180); do
    after="$(curl -s "http://$CONSOLE/cameras" | row_server "$WORKER")"
    [ -n "$after" ] && [ "$after" != "$before" ] && break
    sleep 1
  done
  [ -n "$after" ] || { echo "run $i: $WORKER did not come back within 180 s"; exit 1; }
  t1=$(date +%s)
  secs="$(curl -s "http://$CONSOLE/metrics" | awk -v w="$WORKER" '$0 ~ "vms_failover_seconds" {print $2; exit}')"
  echo "run $i: $WORKER holding its cameras on $after after wall-clock $((t1 - t0))s; vms_failover_seconds ${secs:-?}"
  worst="$(python3 -c "print(max($worst, $((t1 - t0))))")"

  nomad node drain -disable -yes "$old" >/dev/null
  sleep 30
  conflicts="$(curl -s "http://$CONSOLE/metrics" | awk -v w="$WORKER" '$0 ~ "vms_epoch_conflicts\\{worker=\"" w "\"" {print $2; exit}')"
  echo "run $i: $old returned; vms_epoch_conflicts{$WORKER}: ${conflicts:-?}   (the old instance fenced, its events kept under its epoch; the recorder re-subscribed)"
done
echo
echo "failover worst case over $RUNS runs: ${worst}s   <- the datasheet number; the console's vms_failover_seconds is the worker's own measurement"
