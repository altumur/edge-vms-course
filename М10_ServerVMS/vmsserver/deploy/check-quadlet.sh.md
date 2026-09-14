# check-quadlet.sh — validate the Quadlet units in this directory without deploying them (М9 Lesson 4, Step 3)

**Role.** A CI/bench check for the `.container` files beside it. `systemd-analyze verify` cannot check them: it does not know the `[Container]` section and ignores the file. The Podman Quadlet generator's dry run is the tool — it parses every unit in a directory and prints the generated `.service` units, failing on anything it cannot translate. Runs on the bench or in CI (`deploy/check-quadlet.sh`); needs podman's generator on the box. Exit codes: `0` when every unit generates; the generator's own non-zero status on a bad unit (`set -e`); `2` when there is no generator here, with a message on stderr saying to run it on the bench or in CI. `tests/test_deploy_units.py` is the counterpart that runs without podman: it parses the units itself and checks them against the package.

## Step by step

### `set -e`
Any failing command ends the script with its status.

### `DIR="$(cd "$(dirname "$0")" && pwd)"`
The absolute path of the directory the script lives in — the deploy directory — so it can be run from anywhere.

### `GEN=/usr/lib/systemd/system-generators/podman-system-generator`
Where Podman installs the Quadlet generator (the same binary systemd runs at boot to turn `/etc/containers/systemd/*.container` into services).

### `[ -x "$GEN" ] || { echo "no $GEN here (needs podman); run this on the bench or in CI" >&2; exit 2; }`
No generator means no podman on this machine: say so and exit 2, distinct from a validation failure.

### `QUADLET_UNIT_DIRS="$DIR" "$GEN" --dryrun`
Point the generator at this directory instead of the system ones and ask for a dry run: it prints the generated units to stdout and exits non-zero if a file does not parse (an unknown key, a malformed `Volume=`, a missing `Image=`). The `.timer` beside the units is a plain systemd unit and passes through untouched.

## Environment
- `QUADLET_UNIT_DIRS` — set by the script to the deploy directory; overrides the generator's default search path.
