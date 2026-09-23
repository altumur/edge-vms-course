"""The one-box console, standard library — the platform's SpecConsole run
from the VMS spec, plus the two routes only a VMS has (the bytes):

    GET  /timeline/<id>?from&to   segments from the archive resource's manifest, fenced ones marked
    GET  /segment/<path>          the bytes of one promoted segment from this box's archive, Range honoured

Everything else — the page, /spec, /cameras, /where, /marks, /metrics, the
POST/PUT/DELETE of a camera — is `w2cplatform.console.SpecConsole` reading
`vms.subsystem.yaml`; nothing here knows what a camera's fields are. Its own
process (`python3 -m vms console`), with its own token: the operator's rows —
cameras, next_id, retention — and never placement.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # console.py — the one-box console: the platform's SpecConsole over the VMS spec, plus the two media
# routes
# only a VMS has
#
# **Role in the module.** Lesson 6. Everything an operator's console needs to list, create, edit and delete
# cameras, show where they run, export metrics and take marks is `w2cplatform.console.SpecConsole` reading
# `vms.subsystem.yaml` (see `w2cplatform/console.py`); nothing in this file knows what a camera's fields
# are. What the VMS adds is the bytes: `GET /timeline/<id>?from&to` (segments and event buckets from this
# box's archive manifest, fenced ones marked) and `GET /segment/<path>` (one promoted segment, `Range`
# honoured, for the page's `<video>`). They are *registered* as the console's `extra` route function, not
# subclassed. The console is its own process (`python3 -m vms console`, `deploy/console.container`) with
# its own token — `vms/cameras/*`, `vms/next_id`, `vms/retention/*`, `vms/idem/*` — and never placement.
# Depends on `archive.py` (`ArchiveResource`, `Manifest`) and `controller.py`.
#
# ## Module-level names
# - `PAGE`, `send_file` — re-exported from `w2cplatform.console` (`noqa: F401`) for М11, which serves the
#   same page and the same ranged file replies from a Nomad job.
#
# ## Notes
# - `test_the_console_over_http` walks the whole surface through this `serve`: an idempotent POST is one
#   camera; the console's `VmsController` cannot `place` (its token); `PUT {"worker": "w-9"}` is 400;
#   `/cameras` shows `phase running`, `server srv-1`; `/where/1` agrees with the assignments; `/spec` says
#   `rows cameras, media true`; `/metrics` has `vms_cameras_running 1`; a mark lands in
#   `console/<unit>/e1/…` and `subsystems_under(archive)` shows only `console` — never `vms/1/`, whose
#   bucket has one writer; the page never says "camera" outside its HTML comment; then `/timeline/1`, a
#   ranged `/segment/`, a 404, a PUT that bumps `revision` to 2, and a DELETE whose placement waits for
#   `unplace_deleted`.
# - The archive mount in `console.container` is what lets `/segment/` serve bytes; `/data/spool` is
#   mounted read-only there because the console reads and never records.
# ================================================================================================
from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

import json
import time
import urllib.error
import urllib.request

from w2cplatform.console import PAGE, Mount, SpecConsole, heartbeats, holder_of, send_file   # noqa: F401  (PAGE, send_file re-exported for М11)
from w2cplatform.contract import slot_number
from w2cplatform.eventdatabase import MergedIndex
from w2cplatform.spec import Refused, SpecController

from . import volumes
from .archive import ArchiveResource, Manifest, subtract
from .controller import VmsController


class LiveFront:
    """The console's side of live video: the WHEP door. `POST /whep/<cam>`
    creates the fan-out unit `live/streams/<cam>` if nobody is watching yet
    (the console's token may write the operator's rows of the subsystem it
    fronts), finds which gateway the live controller placed it on, and
    proxies the offer there. Nothing here touches a worker, and the console
    never carries media: the answer names the gateway, and the browser's
    RTP goes gateway → browser from then on."""

    def __init__(self, ctl: VmsController, live_ctl: SpecController):
        self.ctl, self.live = ctl, live_ctl

    # Every gateway's last heartbeat: url, capacity, headroom, per-stream status.
    #
    # Deliberately NOT `holders()`: this is the one lookup that wants the stale ones. A gateway that has
    # gone quiet still holds the placement, and proxying to its address gets `OSError` — which the caller
    # turns into "gateway g-2 is not answering — unavailable, not lost". Filter it out here and that
    # becomes "no gateway holds this stream yet", which is a different fact and a worse one: the operator
    # would be told the stream was never placed when in truth its gateway is down.
    def gateways(self) -> dict:
        return heartbeats(self.live.objects, "live/")

    # (gateway name, its URL) for a stream, or (None, None) while unplaced or while the gateway has not heartbeaten.
    def where(self, cam: str) -> tuple[str | None, str | None]:
        pl = self.live.placement(cam)
        if not pl:
            return None, None
        hb = self.gateways().get(pl.worker)
        return pl.worker, (hb.extra.get("url") if hb else None)

    # A viewer's offer. 404 for an unknown camera; the unit is created on the first viewer (a concurrent
    # viewer's "exists" is fine); 503 with retry_after while the live controller has not placed it or the
    # gateway has not heartbeaten; else the gateway's 201 + SDP answer, with Location rewritten to go back
    # through this console (`/whep/session/<id>?gateway=<g>`).
    def offer(self, cam: str, sdp: str, labels: list[str]):
        if self.ctl.camera(cam) is None:
            return 404, {"error": f"no camera {cam}", "detail": f"no camera {cam}"}
        if self.live.unit(cam) is None:
            try:
                self.live.create({"cam": str(cam), "labels": labels})
            except Refused as e:
                if "exists" not in str(e):
                    return 400, {"error": str(e), "detail": str(e)}
        g, url = self.where(cam)
        if not g or not url:
            return 503, {"error": "no gateway holds this stream yet — retry", "detail": "placed on the live controller's next pass", "retry_after": 2}
        req = urllib.request.Request(f"{url}/whep/{cam}", data=sdp.encode(), method="POST", headers={"Content-Type": "application/sdp"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data, status, loc = r.read(), r.status, r.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace").strip()
            if e.code == 404:
                # The gateway is placed and up, and does not know this fan-out yet. That is what a row
                # created a moment ago looks like from here: the controller placed it, and the gateway
                # subscribes on its NEXT pass, up to a couple of seconds away. It comes back as 503 and
                # not as the gateway's own 404 because the two mean the same thing to a browser — wait —
                # and only one of them is a code the page retries. Forwarded verbatim it makes the
                # operator who pressed Live once press it twice, which is the defect the retry exists to
                # prevent. The gateway's own word is kept in `detail`: a 404 "not here" and a 404 "not
                # yet" differ only to whoever is asking.
                return 503, {"error": f"the fan-out is not up on {g} yet — retry",
                             "detail": f"placed on {g}; it subscribes on its next pass", "retry_after": 2}
            return e.code, {"error": f"gateway {g} said {e.code}", "detail": body}
        except OSError:
            return 503, {"error": f"gateway {g} is not answering — unavailable, not lost", "detail": g}
        sid = loc.rsplit("/", 1)[-1]
        return status, data, [("Content-Type", "application/sdp"), ("Location", f"/whep/session/{sid}?gateway={g}")]

    # The viewer hangs up: DELETE proxied to the gateway named in the session URL.
    def hangup(self, sid: str, g: str):
        hb = self.gateways().get(g)
        if not hb or not hb.extra.get("url"):
            return 404, {"error": f"no gateway {g}"}
        req = urllib.request.Request(f"{hb.extra['url']}/whep/session/{sid}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {"error": f"gateway {g} said {e.code}"}
        except OSError:
            return 503, {"error": f"gateway {g} is not answering"}

    # `GET /whep/<cam>`: the stream row, which gateway, and that gateway's status line for it — what the page shows.
    def status(self, cam: str) -> dict:
        g, url = self.where(cam)
        hb = self.gateways().get(g) if g else None
        st = next((s for s in (hb.status if hb else []) if str(s.get("id")) == str(cam)), None)
        return {"cam": cam, "stream": self.live.unit(cam), "gateway": g, "url": url, "status": st}


# Builds the `extra(handler, method, path, q)` function `SpecConsole` calls for every request its built-in
# routes do not claim. Returns `None` ("not ours") for anything but `GET`, or for any request when `archive`
# is `None` (a console without a resource on its server — the second console in
# `test_a_retry_that_lands_on_another_console_is_one_camera` is built that way). Otherwise:
#
# - `GET /segment/<rel>` — `rel` is joined under `archive.root`. If it contains `..` or is not a regular
#   file the reply is `404 {detail, error: "no such segment"}`. Otherwise `send_file(handler, path,
#   "video/mp4")` writes the reply itself (whole, or `206` with `Content-Range` when the request carried
#   `Range`) and `extra` returns `()` — the console's signal that the reply was already served. The test
#   asks `bytes=10-19` of a 256-byte segment and gets exactly those bytes with `Content-Range: bytes
#   10-19/256`; a path that does not exist is 404.
# - `GET /timeline/<id>?from&to` — `int` of the last path segment; `200` with `Manifest(archive.root,
#   cid).timeline(from, to)` — `from` defaults to 0, `to` to `1e12`. Note `current_epoch` is not passed
#   here, so on one box no span is marked `fenced` by this route; the `Manifest.timeline` fencing is
#   exercised directly in `test_lesson3_archive.py`.
# - anything else — `None`, so the console answers 404.
# The holder of a camera, resolved NOW — never written into a span when it was drawn. A camera that moved
# between the drawing and the click would make a stored worker name a 404; resolving at request time costs
# one heartbeat read and cannot go stale. The fourth consumer of the same move, after the recorder, the
# gateway and the detector.
def device_playback(objects, cam, now: float) -> str | None:
    found = holder_of(objects, "vms/", cam, now, field="playback_url")
    return None if found is None else found[2]["playback_url"]


# What the DEVICE has and we do not — drawn only where our own footage does not cover it. The same
# subtraction the recorder fetches by (Lesson 16): one rule, two uses, so the picture and the work cannot
# disagree. A span like this is the one that will disappear — our archive keeps thirty days, a card keeps
# three — which is why the page offers to pin it.
def device_spans(objects, cam, ours: list[dict], t0: float, t1: float, now: float) -> list[dict]:
    found = holder_of(objects, "vms/", cam, now, field="coverage")
    if found is None:
        return []
    cov = found[2]["coverage"]
    want = (max(float(cov["from"]), t0), min(float(cov["to"]), t1))
    if want[1] <= want[0]:
        return []
    have = [(s["start"], s["end"]) for s in ours]
    return [{"start": a, "end": b, "media": None, "epoch": 0, "source": "device",
             "fenced": False, "device": True} for a, b in subtract(want, have)]


# A camera's footage lives under the units that record it, and the archive is keyed by unit (Lesson 7).
# A camera has as many recordings as archives it is written to: one (named `7`, the camera) on a box whose
# only archive is its disks, two (`7` and `7-cloud`) once the footage also goes to a network archive. This
# is the one function that turns a camera into that list, and the timeline below merges what it returns —
# which is why the day the second recording appeared, nothing above this line changed.
def recordings_of(rec_ctl, cam) -> list[str]:
    if rec_ctl is None:
        return [str(cam)]                      # no rec controller mounted: the old assumption, said out loud
    units = [str(r["id"]) for r in rec_ctl.units() if str(r.get("cam", r["id"])) == str(cam)]
    return units or [str(cam)]                 # nothing declared: the camera's own name, so old footage still shows


def vms_routes(archive: ArchiveResource | None, live: LiveFront | None = None, ctl=None, rec_ctl=None):
    """What the VMS adds to the generic console: the media — playback from the
    archive we wrote and from the one we did not, and the WHEP door to the live
    gateways. Returns None when a route is not ours, so the console answers 404."""
    ctl = ctl if ctl is not None else (live.ctl if live is not None else None)
    con_wall = (ctl.wall if ctl is not None else time.time)   # the catalogue needs "now" to know who is reachable

    def extra(handler, method, path, q):
        if live is not None and path.startswith("/whep/"):
            if method == "POST" and not path.startswith("/whep/session/"):
                sdp = handler.rfile.read(int(handler.headers.get("Content-Length", 0))).decode()
                return live.offer(path[len("/whep/"):], sdp, [l for l in q.get("labels", "").split(",") if l])
            if method == "DELETE" and path.startswith("/whep/session/"):
                return live.hangup(path[len("/whep/session/"):], q.get("gateway", ""))
            if method == "GET" and not path.startswith("/whep/session/"):
                return 200, live.status(path[len("/whep/"):])           # GET /whep/<cam>: the stream, its gateway, that gateway's word
            return None
        if method == "POST" and path == "/backfill" and archive is not None:
            # This used to answer 202 and store nothing: the text below was true about what the recorder
            # WOULD do and false about anything having been asked. The request is a row now
            # (`rec/requests/<id>`), the recorder reads it, and the id is deterministic — a retried POST
            # for the same range is the same row, not a second fetch.
            if rec_ctl is None:
                return 503, {"detail": "no recorder subsystem behind this console", "error": "no rec"}
            body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", 0))) or b"{}")
            cam, t0, t1 = str(body.get("cam", "")), float(body.get("from", 0)), float(body.get("to", 0))
            if not cam or t1 <= t0:
                return 400, {"detail": "a backfill wants a camera and a range", "error": "bad range"}
            # WHICH recording gets the missing footage: the one the operator named, or the camera's first.
            # A backfill writes into a unit's tree, and with several recordings of one camera there is no
            # "the" tree any more — the caller says, or takes the first and the answer says which it was.
            units = recordings_of(rec_ctl, cam)
            unit = str(body.get("rec") or "") or units[0]
            if unit not in units:
                return 400, {"detail": f"camera {cam} has no recording {unit}", "error": "no such recording"}
            rid = f"{unit}-{int(t0)}-{int(t1)}"
            rec_ctl.vars.put(rec_ctl.spec.sub.request_key(rid),
                             {"unit": str(unit), "cam": cam, "from": str(t0), "to": str(t1),
                              "at": str(con_wall()), "by": handler.headers.get("X-User", "operator")})
            return 202, {"queued": {"id": rid, "unit": str(unit), "cam": cam, "from": t0, "to": t1},
                         "detail": "the recorder fetches it on its next pass — outside the budget and the window, "
                                   "because a person asked for it"}
        if method != "GET" or archive is None:
            return None
        if (path == "/segment" or path == "/segment/") and ctl is not None:                 # the device's own footage, through its holder
            url = device_playback(ctl.objects, q.get("cam"), con_wall())
            if url is None:
                return 503, {"detail": "nobody holds this camera right now", "error": "unheld"}
            return 200, {"playback": f"{url}?from={q.get('from', 0)}&to={q.get('to', 1e12)}"}
        if path.startswith("/segment/"):
            rel = path[len("/segment/"):]; p = os.path.join(archive.root, rel)
            if ".." in rel or not os.path.isfile(p):
                return 404, {"detail": "no such segment", "error": "no such segment"}
            send_file(handler, p, "video/mp4")
            return ()                                                     # served in full by send_file
        if path.startswith("/timeline/"):
            cid = path.rsplit("/", 1)[1]                                      # a camera, as text: everything below compares as text
            t0, t1 = float(q.get("from", 0)), float(q.get("to", 1e12))
            ours = [sp for unit in recordings_of(rec_ctl, cid)                    # every recording of this camera…
                    for sp in Manifest(archive.root, unit).timeline(t0, t1)]      # …merged into one timeline
            extra = device_spans(ctl.objects, cid, ours, t0, t1, con_wall()) if ctl is not None else []
            return 200, sorted(ours + extra,
                               key=lambda d: (d["start"], d["epoch"]))
        return None
    return extra


# `SpecConsole(ctl, marks_root=archive.root if archive else None, wall=wall, extra=vms_routes(archive),
# media=archive is not None)`. With an archive: operator marks (`POST /marks`) go into the console's own
# event log under `console/<hostname:pid>/e1/` on this server's resource, and `/spec` reports `media: true`
# so the page draws a timeline and a player. Without one: no marks (503) and no media.
# What the `rec` mount adds to the generic console: the archives themselves. `GET /rec/volumes` is the
# operator's answer to "where can this footage go, and is anybody writing there"; the POST and the DELETE
# are the declaration. Three numbers ride along, and they are the point of the screen:
#
#   wanted  — declared archives that are enabled: how many recorder processes this cluster needs
#   serving — how many of them a live recorder is actually holding
#   spare   — processes running with no volume, ready to take the next one declared
#
# `spare: 0` with `serving < wanted` is the one state that needs a person: an archive was declared and
# there is no process free to serve it. The console says so; it does not start one. Starting processes is
# the scheduler's, here as everywhere — what the platform owes is the number, not the action.
# How many recorder processes are missing, and THE COMMAND that starts them — written out, for the
# operator to run. Not a button: a console with a token to its orchestrator would be a console that can
# start processes, which is the one thing this whole design keeps out of the platform (М10A, lesson 7).
# What it can do is stop making the operator translate. It knows how many are missing (an archive is
# declared and nobody, spare included, can take it), and it knows which orchestrator started IT —
# `NOMAD_ALLOC_ID` in its own environment — so it can write the line out in that orchestrator's words.
#
# The slot name for a box is the next free number: instances are named by the slot they claim
# (`recworker@r-3` claims `r-3`), so the command has to name one nobody holds.
def scale_hint(rec_ctl: SpecController, unserved: int, spare: int) -> dict:
    needed = max(0, unserved - spare)                   # a spare takes an archive on its next pass
    if not needed:
        return {"needed": 0, "how": None}
    if os.environ.get("NOMAD_ALLOC_ID"):
        live = len(rec_ctl.workers_seen())
        return {"needed": needed, "how": f"nomad job scale recworker {live + needed}"}
    taken = list(rec_ctl.slots())
    nxt = max([slot_number(n) for n in taken] + [0]) + 1
    prefix = (taken[0].rsplit("-", 1)[0] if taken and "-" in taken[0] else "r")
    return {"needed": needed,
            "how": " && ".join(f"systemctl start recworker@{prefix}-{nxt + i}" for i in range(needed))}


# The two numbers a scaling policy needs and nothing else has. `declared` is how many archives the
# operator says should be written into; `unserved` is how many of those nobody is holding — which, added
# to the recorders that are live, is the count this job must reach.
#
# Why here and not in the platform's `metrics_text`: it counts VOLUMES, and the platform has never heard
# of one. `metrics_extra` is the seam, the same shape as `extra` for routes.
def rec_metrics(rec_ctl: SpecController):
    def lines() -> list[str]:
        view = volumes.served(rec_ctl.vars, rec_ctl.spec.sub, rec_ctl.wall(), objects=rec_ctl.objects)
        unserved = view["wanted"] - view["serving"]
        spare = sum(1 for w in rec_ctl.workers_seen() if rec_ctl.place_of(w) == "")
        return ["# TYPE rec_volumes_declared gauge",
                f"rec_volumes_declared {view['wanted']}",
                "# TYPE rec_volumes_unserved gauge",
                f"rec_volumes_unserved {unserved}",
                # The number a scaling policy must use, and the reason it is not `unserved` itself: an
                # archive nobody CAN take does not become takeable by starting processes. A local volume
                # declared for a server where the scheduler puts no recorder leaves `unserved` at one for
                # ever, and a policy reading that would ask for one more worker, then another, up to its
                # ceiling — every one of them a spare that cannot help. Subtracting the spares stops it
                # after the first: one free process proves the shortage is not a shortage of processes.
                "# TYPE rec_recorders_needed gauge",
                f"rec_recorders_needed {max(0, unserved - spare)}"]
    return lines


def rec_routes(rec_ctl: SpecController):
    def extra(handler, method, path, q):
        if not path.startswith("/volumes"):
            return None
        if method == "GET" and path in ("/volumes", "/volumes/"):
            now = rec_ctl.wall()
            view = volumes.served(rec_ctl.vars, rec_ctl.spec.sub, now, objects=rec_ctl.objects)
            spare = [w for w in rec_ctl.workers_seen() if rec_ctl.place_of(w) == ""]
            return 200, {**view, "spare": len(spare), "spares": sorted(spare),
                         # `live` so that whatever acts on `needed` does not have to ask the orchestrator
                         # how many recorders are running — the console already knows, from heartbeats,
                         # and one source for the pair means the two numbers cannot disagree.
                         "live": len(rec_ctl.workers_seen()),
                         **scale_hint(rec_ctl, view["wanted"] - view["serving"], len(spare)),
                         # …and what to offer somebody who has declared nothing yet: the disk each box
                         # already records into, sized to its partition. A proposal, not a row.
                         "suggested": volumes.suggest(rec_ctl.vars, rec_ctl.objects, rec_ctl.spec.sub, now)}
        if method == "POST" and path in ("/volumes", "/volumes/"):
            body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", 0))) or b"{}")
            try:
                vol = volumes.write(rec_ctl.vars, body)
            except Refused as e:
                return 400, {"detail": str(e), "error": "refused"}
            return 201, {"volume": {k: v for k, v in {**vol.to_items(), "name": vol.name}.items()
                                    if not k.endswith("_secret")}}
        if method == "DELETE" and path.startswith("/volumes/"):
            name = path[len("/volumes/"):]
            if not any(v.name == name for v in volumes.declared(rec_ctl.vars)):
                return 404, {"detail": f"no volume {name}", "error": "no such volume"}
            # The row goes; the recorder holding it finds out on its next pass, stops what it was writing
            # there and takes something else. The FOOTAGE is not touched — deleting the declaration is not
            # deleting the archive, and the two must not be one button.
            volumes.delete(rec_ctl.vars, name)
            return 200, {"deleted": name, "detail": "the recorder stops writing there on its next pass; "
                                                    "the footage already written is untouched"}
        return None
    return extra


def make_console(ctl: VmsController, archive: ArchiveResource | None, wall=None, live_ctl: SpecController | None = None,
                 mounts: dict[str, SpecController] | None = None, index=None) -> Mount:
    """One console process for the box: the VMS at `/` (the page, /cameras, the media routes, the WHEP door),
    and every other subsystem the console fronts under its name — `/live/…`, `/det/…` — each a SpecConsole over
    that subsystem's spec with the console's token. `live_ctl` opens the WHEP door and is mounted at /live;
    `mounts` adds the rest by name."""
    live = LiveFront(ctl, live_ctl) if live_ctl is not None else None
    index = index or MergedIndex(ctl.objects, wall=wall or time.time)   # no database here: the resource process's, asked over HTTP
    rec_ctl = (mounts or {}).get("rec")                                  # the console fronts it anyway: the page's Record toggle
    root = SpecConsole(ctl, marks_root=archive.root if archive else None, wall=wall,
                       extra=vms_routes(archive, live, ctl, rec_ctl), media=archive is not None, index=index)
    m = Mount(root)
    if live_ctl is not None:
        m.mount("live", SpecConsole(live_ctl, wall=wall, index=index))
    for name, c in (mounts or {}).items():
        m.mount(name, SpecConsole(c, wall=wall, index=index,             # every mount answers /events from the same merge
                                  extra=rec_routes(c) if name == "rec" else None,    # …and `rec` answers for the archives too
                                  metrics_extra=rec_metrics(c) if name == "rec" else None))
    return m


# `make_console(...).serve(host, port)`: the server in a daemon thread, returned so the caller can
# `shutdown()` it. `__main__.console` calls it with `$CONSOLE_HOST:$CONSOLE_PORT`; the tests with `port=0`.
def serve(ctl: VmsController, archive: ArchiveResource | None, host: str = "127.0.0.1", port: int = 8080, wall=None,
          live_ctl: SpecController | None = None, mounts: dict[str, SpecController] | None = None, index=None) -> ThreadingHTTPServer:
    return make_console(ctl, archive, wall, live_ctl, mounts, index).serve(host, port)
