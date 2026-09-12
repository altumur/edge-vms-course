#!/bin/sh
# М11 — the Go port measured against the Python original. Builds the Go
# recorder, runs both idle baselines, both test suites, both micro-benchmarks.
# Needs: go, python3, RECORDER_PATH (../recorder) and CLUSTERVMS_PATH (../clustervms).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
export RECORDER_PATH="${RECORDER_PATH:-$HERE/../../М9_EdgeVMS/recorder}"
export CLUSTERVMS_PATH="${CLUSTERVMS_PATH:-$HERE/../clustervms}"
cd "$HERE"
echo "== binaries"
CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/clustervms ./cmd/clustervms
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/clustervms-arm64 ./cmd/clustervms
CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/clustervms-baseline ./cmd/baseline
echo "go node (x86-64, static, without pgx): $(du -k /tmp/clustervms | cut -f1) kB"
echo "go node (arm64, cross-compiled in one command): $(du -k /tmp/clustervms-arm64 | cut -f1) kB"
echo "== idle, 50 cameras, every task running (PSS)"
for i in 1 2 3; do /tmp/clustervms-baseline; done
for i in 1 2 3; do python3 cmd/pybaseline/baseline.py; done
echo "== test suites (wall clock)"
ms() { echo $(( ($(date +%s%N) - $1) / 1000000 )); }
t=$(date +%s%N); go test -count=1 ./cluster/ >/dev/null; echo "go: 30 tests in $(ms $t) ms (go test, compile included)"
t=$(date +%s%N); go test -count=1 -exec true ./cluster/ >/dev/null; c=$(ms $t)
t=$(date +%s%N); go test -count=1 ./cluster/ >/dev/null; echo "   of which the tests themselves: $(( $(ms $t) - c )) ms"
t=$(date +%s%N); python3 "$CLUSTERVMS_PATH/tests/run.py" >/dev/null; echo "python: 29 tests in $(ms $t) ms"
echo "== the controller's work, per operation"
go test -run '^$' -bench . -benchmem ./cluster/ | grep -E '^Benchmark' | sed 's/-[0-9]* / /'
python3 cmd/pybench/bench.py
