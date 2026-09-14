# check-quadlet.sh — validate the Quadlet units with Podman's generator dry-run, not systemd-analyze

**Role.** Lesson 4, Step 3. Checks every `.container` file in this directory without deploying it. `systemd-analyze verify` cannot do this: it does not know the `[Container]` section and ignores the file. The tool is the Quadlet generator itself in `--dryrun` mode, and the lesson says it belongs in CI. POSIX `sh`, `set -e`. Exit 2 if the generator binary is absent (needs Podman; run on the bench or in CI); otherwise the generator's status — non-zero on a unit it cannot translate.

## Step by step
### Locate
`DIR` is the script's own directory; `GEN` is `/usr/lib/systemd/system-generators/podman-system-generator`. If `GEN` is not executable, print where to run it instead and exit 2.

### Dry-run
`QUADLET_UNIT_DIRS="$DIR" "$GEN" --dryrun` — the generator reads unit files from the directories in that variable instead of `/etc/containers/systemd`, and prints the `.service` units it would write to stdout. Reading the output is the review: `Image=`, `Volume=`, `PublishPort=` become `podman run` arguments, and `[Install]` becomes the enablement symlink the generator itself applies (which is why `vms-agent.container` warns never to `systemctl enable` a Quadlet unit).

## Notes
- The README lists the Quadlet units under "written to the documentation, not executed here" and names this script as the check.
