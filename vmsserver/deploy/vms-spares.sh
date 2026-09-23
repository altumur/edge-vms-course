#!/bin/sh
# vms-spares.sh — start a recorder for every declared archive nobody is holding.
#
# The knob for the two installations the Autoscaler does not serve: one box, and a cluster with no
# metrics store. It runs on the HOST, under root, from `vms-spares.timer` — and that placement is the
# whole point of the file. Starting a unit means talking to systemd as root; a console that could do it
# would be a console holding root on its own machine, and "the platform does not start processes" would
# be a sentence with an exception in it. So the console publishes the NUMBER and this script, which the
# operator installed deliberately, does the starting.
#
# Two rules it follows, and the second is not optional:
#
#   1. It asks for a number, not for a command. `/rec/volumes` also returns `how` — the line written out
#      for a person to read — and this script ignores it. Running a string that arrived over HTTP as root
#      is remote code execution with extra steps, however friendly the source.
#   2. It has a ceiling of its own (`MAX_RECORDERS`). A bug on the other side that reported "nine hundred
#      missing" must cost a log line, not nine hundred containers.
#
# It never stops anything. A spare costs a few megabytes and is what makes the NEXT archive get served in
# a pass instead of a deploy; deciding that a box has too many is a person's call, on purpose.
set -eu

CONSOLE="${CONSOLE:-http://127.0.0.1:8080}"
MAX_RECORDERS="${MAX_RECORDERS:-8}"
PREFIX="${RECORDER_PREFIX:-r}"

needed=$(curl -fsS --max-time 5 "$CONSOLE/rec/volumes" 2>/dev/null \
         | python3 -c 'import json,sys; print(max(0, int(json.load(sys.stdin).get("needed") or 0)))' 2>/dev/null) || {
  echo "vms-spares: no answer from $CONSOLE — nothing to do" >&2
  exit 0                                        # the console being down is not a reason to start anything
}

[ "$needed" -gt 0 ] || exit 0
echo "vms-spares: $needed archive(s) declared with nobody to hold them"

i=1
started=0
while [ "$started" -lt "$needed" ] && [ "$i" -le "$MAX_RECORDERS" ]; do
    unit="recworker@${PREFIX}-${i}"
    if ! systemctl is-active --quiet "$unit"; then
        if systemctl start "$unit"; then
            echo "vms-spares: started $unit"
            started=$((started + 1))
        fi
    fi
    i=$((i + 1))
done

if [ "$started" -lt "$needed" ]; then
    echo "vms-spares: $((needed - started)) still unserved — no free slot under MAX_RECORDERS=$MAX_RECORDERS," \
         "or the unit did not start (see journalctl -u 'recworker@*')" >&2
fi
