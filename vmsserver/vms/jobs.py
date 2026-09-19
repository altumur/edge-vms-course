"""The console's side of a subsystem whose work ENDS.

A worker is the only participant that can know a job is finished: the plan and
the progress are files on the server it ran on, and its ACL —
`[<name>/epoch/*, <name>/slots/*]` — forbids it to write the row. A controller
could write it (its grant is `<name>/*`), but then one row has two writers and
they collide exactly when the operator edits a job the controller is finishing.

So the console does it, in a pass of its own beside the blob sweep: it reads the
heartbeats it already reads, and moves the row's `state` when the worker holding
the job says the work is over. One writer per row, and the operator sees `done`
in the row they created.

Why the state must be DURABLE and not simply read off the heartbeat, when the
placement predicate lands (Lesson 21): un-placing a finished job makes its worker
drop it, which makes the next heartbeat stop mentioning it, which makes the
evidence of `done` disappear — and the job is placed again, and scans the archive
from the top, for ever. A finished job has to be finished somewhere that does not
depend on its still being assigned.
"""
from __future__ import annotations

import logging

TERMINAL = ("done", "failed")
log = logging.getLogger("vms.jobs")


# One pass: `{done: n, failed: n}` — how many rows this pass moved.
#
# Only the worker the CONTROLLER placed the job on is believed. A heartbeat object outlives its worker,
# so a slot that finished this job yesterday, before it was moved to another server, still says `done`
# in the store; taking that at face value would end a scan that is running right now, somewhere else,
# from the beginning.
def reap(ctl) -> dict:
    moved = {"done": 0, "failed": 0}
    placed: dict[str, str] = {}
    for row in ctl.units():
        if str(row.get("state", "")) in TERMINAL:
            continue                                        # already moved; the predicate un-places it, not us
        p = ctl.placement(row["id"])
        if p is not None:
            placed[str(row["id"])] = p.worker
    for st in ctl.read_model():
        uid, phase = str(st.get("id")), str(st.get("phase", ""))
        if phase not in TERMINAL or placed.get(uid) != st.get("worker"):
            continue
        ctl.update(uid, {"state": phase})
        moved[phase] += 1
        log.info("%s %s: %s (%.0f s of footage, %d event(s))", ctl.spec.name, uid, phase,
                 float(st.get("covered", 0)), int(st.get("events", 0)))
    return moved
