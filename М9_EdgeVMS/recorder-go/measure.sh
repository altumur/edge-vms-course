#!/bin/sh
# М9 Lesson 9, Step 5 — the rewrite argument as a number. Runs both
# controller baselines at idle and prints PSS, plus binary sizes.
# Needs: go, python3, and RECORDER_PATH pointing at ../edgevms/recorder.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
export RECORDER_PATH="${RECORDER_PATH:-$HERE/../edgevms/recorder}"
cd "$HERE"
CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/recorder-baseline ./cmd/baseline
GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -ldflags="-s -w" -o /tmp/recorder-baseline-arm64 ./cmd/baseline
echo "go binary (x86-64, static): $(du -k /tmp/recorder-baseline | cut -f1) kB"
echo "go binary (arm64, cross-compiled in one command): $(du -k /tmp/recorder-baseline-arm64 | cut -f1) kB"
for i in 1 2 3; do /tmp/recorder-baseline; done
for i in 1 2 3; do python3 cmd/pybaseline/baseline.py; done
