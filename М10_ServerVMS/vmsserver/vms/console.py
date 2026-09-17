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
from w2cplatform.eventdatabase import MergedIndex
from w2cplatform.spec import Refused, SpecController

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
        if self.ctl.camera(int(cam)) is None:
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
            return e.code, {"error": f"gateway {g} said {e.code}", "detail": e.read().decode(errors="replace")}
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
# With `id: cam` there is exactly one such unit and its name is the camera's number — so this returns
# `["7"]` for camera 7 and the answer is the one the console always gave. The day a camera has two
# recordings it returns both, and the timeline below merges them without another line changing.
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
            body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", 0))) or b"{}")
            return 202, {"queued": {"cam": body.get("cam"), "from": body.get("from"), "to": body.get("to")},
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
            cid = int(path.rsplit("/", 1)[1])
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
        m.mount(name, SpecConsole(c, wall=wall, index=index))            # every mount answers /events from the same merge
    return m


# `make_console(...).serve(host, port)`: the server in a daemon thread, returned so the caller can
# `shutdown()` it. `__main__.console` calls it with `$CONSOLE_HOST:$CONSOLE_PORT`; the tests with `port=0`.
def serve(ctl: VmsController, archive: ArchiveResource | None, host: str = "127.0.0.1", port: int = 8080, wall=None,
          live_ctl: SpecController | None = None, mounts: dict[str, SpecController] | None = None, index=None) -> ThreadingHTTPServer:
    return make_console(ctl, archive, wall, live_ctl, mounts, index).serve(host, port)
