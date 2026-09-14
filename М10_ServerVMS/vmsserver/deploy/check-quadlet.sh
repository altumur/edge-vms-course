#!/bin/sh
# М9 Lesson 4, Step 3, for these units — check Quadlet files without deploying
# them. `systemd-analyze verify` cannot: it does not know [Container] and
# ignores the file. The generator's dry-run is the tool, and it belongs in CI.
#   deploy/check-quadlet.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
GEN=/usr/lib/systemd/system-generators/podman-system-generator
[ -x "$GEN" ] || { echo "no $GEN here (needs podman); run this on the bench or in CI" >&2; exit 2; }
QUADLET_UNIT_DIRS="$DIR" "$GEN" --dryrun
