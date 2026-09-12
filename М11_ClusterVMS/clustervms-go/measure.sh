#!/bin/sh
# М11 — the Go port measured against the Python original. Builds the Go
# binaries, runs both idle baselines, both test suites, both micro-benchmarks.
# Needs: go, python3, CLUSTERVMS_PATH (../clustervms) — М10's Python package is found from there.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
export CLUSTERVMS_PATH="${CLUSTERVMS_PATH:-$HERE/../clustervms}"
export VMSSERVER_PATH="${VMSSERVER_PATH:-$HERE/../../М10_ServerVMS/vmsserver}"
cd "$HERE"
echo "== binaries"
CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/clustervms ./cmd/clustervms
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/clustervms-arm64 ./cmd/clustervms
CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/clustervms-baseline ./cmd/baseline
echo "go cluster jobs (x86-64, static): $(du -k /tmp/clustervms | cut -f1) kB"
echo "go cluster jobs (arm64, cross-compiled in one command): $(du -k /tmp/clustervms-arm64 | cut -f1) kB"
echo "== idle, 50 cameras, one worker and one controller (PSS)"
for i in 1 2 3; do /tmp/clustervms-baseline; done
for i in 1 2 3; do python3 cmd/pybaseline/baseline.py; done
echo "== test suites (wall clock)"
ms() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }
t=$(date +%s%N); go test -count=1 ./cluster/ >/dev/null; echo "go: 29 tests in $(ms $t) ms (go test, compile included)"
t=$(date +%s%N); go test -count=1 -exec true ./cluster/ >/dev/null; c=$(ms $t)
t=$(date +%s%N); go test -count=1 ./cluster/ >/dev/null; echo "   of which the tests themselves: $(( $(ms $t) - c )) ms"
t=$(date +%s%N); python3 "$CLUSTERVMS_PATH/tests/run.py" >/dev/null 2>&1; echo "python: 29 tests in $(ms $t) ms"
echo "== the controller's work, per operation"
go test -run '^$' -bench . -benchmem ./cluster/ | grep -E '^Benchmark' | sed 's/-[0-9]* / /'
python3 cmd/pybench/bench.py
