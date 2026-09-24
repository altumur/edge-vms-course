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
# What a job's row may say, and what the worker's phase is allowed to move it to. The console mirrors the
# phase into the row not because the row is a better heartbeat — it is a worse one — but because the row is
# what SURVIVES the job being un-placed, and what the operator opened. `waiting` and `unsupported` are the
# worker's news and stay in the heartbeat: nothing downstream acts on them.
MIRRORED = ("fetching", "running") + TERMINAL
log = logging.getLogger("vms.jobs")


# The other half of `<name>/requests/<id>`: the worker fetched it and said so in its heartbeat; the row goes.
#
# Same division as `reap` below, and for the same reason — a worker writes no configuration. Here it is
# cheaper still, because a request has no state to move: once the work named in it is done the row has
# nothing left to say, and a store that keeps every range anyone ever asked for is a store that grows
# without anybody deciding it should.
# "Record this camera for ten minutes" — a request turned into a row, by the one token that may write
# rows: the CONSOLE's. Run in the console process's loop, beside the reaper that clears requests.
#
# The division is the same as everywhere and it is the reason this function is here rather than in the
# recorder or in the controller. A worker writes no configuration, and a recording IS configuration — it
# has an id, a retention, a home and a placement. The controller writes placement and not rows. What is
# left is the console, which is the operator's agent, and a scenario asking for ten minutes of a camera is
# the operator asking through something they wrote.
#
# The row is named `<cam>-auto`, which is the naming rule the page already uses one field over: a camera
# recorded by hand and by a scenario has two recordings, two trees and two retentions, and neither
# surprises the other. A second request while it runs EXTENDS it — ten more minutes from now — instead of
# making `<cam>-auto-2`: the scenario meant "keep recording", not "record twice".
def record_on_request(rec_ctl, now: float) -> int:
    started = 0
    for key in sorted(rec_ctl.vars.list(rec_ctl.sub.requests_prefix())):
        it, _ = rec_ctl.vars.get(key)
        if not it or str(it.get("action", "")) != "record":
            continue                                        # a backfill: the recorder's, not ours
        rid = key.rsplit("/", 1)[1]
        until = float(it.get("valid_until", 0) or 0)
        if until and now > until:
            rec_ctl.vars.delete(key)                        # asked for too late to mean what it meant
            log.warning("%s: %s expired before it was turned into a recording", rec_ctl.spec.name, rid)
            continue
        cam = str(it.get("cam") or it.get("unit") or "")
        minutes = float(it.get("minutes", 0) or 0)
        if not cam or minutes <= 0:
            rec_ctl.vars.delete(key)
            log.warning("%s: %s asks to record nothing: %s", rec_ctl.spec.name, rid, it)
            continue
        name, ends = f"{cam}-auto", now + minutes * 60
        row = rec_ctl.unit(name)
        try:
            if row is None:
                fields = {"name": name, "cam": cam, "until": ends}
                if it.get("archive"):
                    fields["home"] = str(it["archive"])
                rec_ctl.create(fields)
                started += 1
            elif float(row.get("until") or 0) < ends:
                rec_ctl.update(name, {"until": ends})       # keep recording, not record twice
                started += 1
        except Exception as e:                              # noqa: BLE001 — a refusal is an answer, and it is ours to log
            log.warning("%s: %s could not start %s: %s", rec_ctl.spec.name, rid, name, e)
        rec_ctl.vars.delete(key)                            # performed or refused, it has nothing left to say
    return started


# …and the other end of it. A recording with an `until` in the past is over: the row goes, the controller
# unplaces it, the recorder stops the pipeline. The ordinary path for a recording somebody removed,
# reached by a clock instead of by a click.
#
# `until: 0` is every recording an operator made by hand, and this function never touches one.
def expire_recordings(rec_ctl, now: float) -> int:
    gone = 0
    for row in rec_ctl.units():
        until = float(row.get("until") or 0)
        if until and now > until:
            rec_ctl.delete(row["id"])
            gone += 1
            log.info("%s: %s reached its end", rec_ctl.spec.name, row["id"])
    return gone


def clear_requests(ctl) -> int:
    from w2cplatform.console import heartbeats
    fetched: set[str] = set()
    for _, hb in heartbeats(ctl.objects, ctl.spec.name + "/").items():
        fetched |= {r for r in str(hb.extra.get("fetched", "")).split(",") if r}
    gone = 0
    for key in ctl.vars.list(ctl.sub.requests_prefix()):
        if key.rsplit("/", 1)[1] in fetched:
            ctl.vars.delete(key)
            gone += 1
    return gone


# One pass: `{done: n, failed: n}` — how many rows this pass moved.
#
# Only the worker the CONTROLLER placed the job on is believed. A heartbeat object outlives its worker,
# so a slot that finished this job yesterday, before it was moved to another server, still says `done`
# in the store; taking that at face value would end a scan that is running right now, somewhere else,
# from the beginning.
def reap(ctl) -> dict:
    moved = {"done": 0, "failed": 0}
    placed: dict[str, str] = {}
    state: dict[str, str] = {}
    for row in ctl.units():
        if str(row.get("state", "")) in TERMINAL:
            continue                                        # already moved; the predicate un-places it, not us
        state[str(row["id"])] = str(row.get("state", ""))
        p = ctl.placement(row["id"])
        if p is not None:
            placed[str(row["id"])] = p.worker
    for st in ctl.read_model():
        uid, phase = str(st.get("id")), str(st.get("phase", ""))
        if phase not in MIRRORED or placed.get(uid) != st.get("worker"):
            continue
        if str(state.get(uid, "")) == phase:
            continue                                        # already says it: a row that moves every pass is a
                                                            # revision that moves every pass, for every reader downstream
        ctl.update(uid, {"state": phase})
        if phase in TERMINAL:
            moved[phase] += 1
            log.info("%s %s: %s (%.0f s of footage, %d event(s))", ctl.spec.name, uid, phase,
                     float(st.get("covered", 0)), int(st.get("events", 0)))
    return moved


# A job that cannot run because the footage is still on the device: ask the recorder for it.
#
# The scan does NOT read the device itself, and that is the design and not a shortcut. The playback door
# admits two sessions per device (М10B Lesson 15), and those two belong to the operator watching the gap
# and to the recorder saving it; a scan is exactly the greedy third. Worse, a card keeps three days: a
# search that races the device's own retention can find a car whose evidence is gone by the time anyone
# clicks. So the range is fetched ONCE, into our archive, with our epoch and our retention — and the scan
# then runs over footage we own, with `vms/scan.py` unchanged.
#
# The request id is the range, so a job asking every thirty seconds writes one row, not a queue.
def ask_for_footage(job_ctl, rec_ctl) -> int:
    asked = 0
    for st in job_ctl.read_model():
        if str(st.get("phase", "")) != "fetching":
            continue
        unit, t0, t1 = str(st.get("rec", "")), float(st.get("from", 0)), float(st.get("to", 0))
        if not unit or t1 <= t0:
            continue
        rid = f"{unit}-{int(t0)}-{int(t1)}"
        key = rec_ctl.sub.request_key(rid)
        it, _ = rec_ctl.vars.get(key)
        if it:
            continue                                        # already asked; the recorder clears it when it is fetched
        rec_ctl.vars.put(key, {"unit": unit, "cam": str(st.get("cam", unit)), "from": str(t0), "to": str(t1),
                               "at": str(job_ctl.wall()), "by": f"{job_ctl.spec.name}/{st.get('id')}"})
        asked += 1
        log.info("%s %s: asking the recorder for %s [%.0f, %.0f)", job_ctl.spec.name, st.get("id"), unit, t0, t1)
    return asked


# What the recorder just fetched from a device is a hole in the DETECTIONS too: nothing was watching the
# camera while nothing was recording it. This closes the second hole with the first.
#
# One scan per (range × detector), because "no hole" means every model that runs live on that camera also
# ran over those minutes — and with the SAME settings, so `params` and `mask` are copied from the detector's
# own row rather than re-entered.
#
# The id is the range and the detector, so the pass is idempotent by construction: the recorder keeps
# reporting a range for as long as it stays in its window, and the second pass finds the row already there.
# A row an operator DELETED stays deleted — the marker outlives the row, which is what `vars.get` sees and
# `unit()` does not.
def scan_what_arrived(rec_ctl, det_ctl, job_ctl) -> int:
    from w2cplatform.console import heartbeats
    made = 0
    for _, hb in heartbeats(rec_ctl.objects, rec_ctl.spec.name + "/").items():
        for span in str(hb.extra.get("closed", "")).split(","):
            parts = span.split("|")
            if len(parts) != 3:
                continue
            unit, t0, t1 = parts[0], float(parts[1]), float(parts[2])
            rec = rec_ctl.unit(unit)
            if rec is None or t1 <= t0:
                continue
            cam = str(rec.get("cam", unit))
            for d in det_ctl.units():
                if str(d.get("cam", "")) != cam or not d.get("enabled", True):
                    continue
                jid = f"{unit}-{d['kind']}-{int(t0)}-{int(t1)}"
                if job_ctl.vars.get(job_ctl.sub.config(job_ctl.spec.rows, jid))[0]:
                    continue                                # made already, or deleted on purpose
                body = {"name": jid, "cam": cam, "rec": unit, "kind": str(d["kind"]),
                        "from": t0, "to": t1}
                for f in ("params", "mask"):                # the same settings the live detector runs with
                    if d.get(f):
                        body[f] = d[f]
                job_ctl.create(body)
                made += 1
                log.info("%s: scanning %s [%.0f, %.0f) with %s — the footage arrived from a device",
                         job_ctl.spec.name, unit, t0, t1, d["kind"])
    return made


# The fourth way to use a device archive: watch everything, keep what a model liked.
#
# The survey reports the stretches; this turns them into the request the recorder already understands. The
# same division as everywhere here — the worker knows and may not write, the console writes — and the same
# reason the request's id is the range: a pass every thirty seconds must write one row, not a queue.
#
# What lands in `rec/<cam>/` this way is ordinary footage with the ordinary retention, and that is the
# point rather than an omission: when only the interesting minutes are copied, everything on the server is
# interesting, and "evidence" needs no second archive and no second lifetime.
def keep_what_fired(survey_ctl, rec_ctl) -> int:
    from w2cplatform.console import heartbeats
    asked = 0
    for _, hb in heartbeats(survey_ctl.objects, survey_ctl.spec.name + "/").items():
        for span in str(hb.extra.get("hits", "")).split(","):
            parts = span.split("|")
            if len(parts) != 3:
                continue
            cam, t0, t1 = parts[0], float(parts[1]), float(parts[2])
            if t1 <= t0:
                continue
            unit = next((str(r["id"]) for r in rec_ctl.units() if str(r.get("cam", r["id"])) == cam), None)
            if unit is None:
                continue                                # nothing on this server records that camera: nowhere to put it
            rid = f"{unit}-{int(t0)}-{int(t1)}"
            key = rec_ctl.sub.request_key(rid)
            if rec_ctl.vars.get(key)[0]:
                continue                                # already asked; the recorder clears it when it is fetched
            rec_ctl.vars.put(key, {"unit": unit, "cam": cam, "from": str(t0), "to": str(t1),
                                   "at": str(survey_ctl.wall()), "by": f"{survey_ctl.spec.name}/{cam}"})
            asked += 1
            log.info("%s: keeping %s [%.0f, %.0f) — a model liked it", survey_ctl.spec.name, unit, t0, t1)
    return asked
