"""The console as data — the other half of spec.py. A subsystem's YAML
already says what its units are, which fields the operator owns and what
leaves the cluster; that is everything a console needs to list, edit and
show them. So the console is one class, run from the same spec:

    GET  /                       the page (console.html): the unit list, the edit form built from the spec's fields,
                                 and — when the subsystem registered media routes — a timeline and a player
    GET  /spec                   what the page reads first: name, rows, id rule, fields, media, metric names
    GET  /<rows>                 {rows: the read model from every worker's heartbeat, configured: the units}
    GET  /where/<id>             the stored placement (why) and the assignments' answer (where, one scan)
    GET  /resources              the platform's resources: usage, units, live | silent
    GET  /unplaceable            units nothing live can serve, with the labels that say why
    GET  /servers                every server as placement sees it: its archive (the label), its resource (the fact), its workers, placeable or why not
    GET  /events?from&to&unit&kind&subsystem   the resources' event databases, merged (MergedIndex), fenced by every subsystem's epochs
    GET  /metrics                <name>_workers_live · <name>_worker_headroom{worker,server} · <name>_worker_load ·
                                 <name>_epoch_conflicts · <name>_failover_seconds{kind="worst"} · <name>_resources_live ·
                                 <name>_<running> (units in phase "running"; the spec names the gauge)
    POST /<rows>  (Idempotency-Key)   the row only — the controller places it on its next pass; the key is a Variable
                                 (<sub>/idem/<key>), so the retry is answered the same by whichever console gets it
    PUT  /<rows>/<id>            the operator's fields; a new revision; refused where the controller refuses
    DELETE /<rows>/<id>          the row is marked; the controller's pass takes its placement back
    POST /marks  (Idempotency-Key)    an operator's observation {unit|cam, note}: the CONSOLE's event, into
                                 console/<instance>/… on this server's resource — never a worker's bucket

What a subsystem adds is registered, not subclassed: `extra(handler, method,
path, query) -> reply | None` gets every request the routes above do not
claim (the VMS: /timeline and /segment); a reply is `(status, dict)`,
`(status, bytes)`, `(status, bytes, headers)` or `()` when the extra wrote
it itself. The console holds the subsystem's
SpecController with the console's token — the operator's rows, never
placement — so a write it should not make is a 403 from the store, not a
rule in this file.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # console.py — the console as data: SpecConsole over the same spec, with idempotent writes and the
# subsystem's registered extra routes
#
# **Role in the module.** Lesson 5, the other half of `spec.py`. A subsystem's YAML already says what its
# units are, which fields the operator owns and what leaves the cluster; that is everything a console needs
# to list, edit and show them, so the console is one class run from the same spec. It serves `console.html`,
# `/spec` (what the page reads first), the rows with the read model, `/where`, `/resources`, `/unplaceable`,
# `/events` (if a `MergedIndex` — anything with `query(t0, t1, cam, kind, subsystem, unit, current_epochs)` — is behind it), `/metrics`, and the writes — POST/PUT/DELETE on the rows and
# POST `/marks` — with idempotency keys stored in Variables so a retry answered by another console instance
# is the same request. What a subsystem adds is registered, not subclassed: `extra(handler, method, path,
# query)` gets every request the built-in routes do not claim (the VMS: `/timeline` and `/segment`). The
# console holds the subsystem's `SpecController` with the *console's* token (the operator's rows, never
# placement), so a write it should not make is a 403 from the store, not a rule in this file.
# `vms/console.py` builds it via `make_console`; the deploy unit `vmsconsole.container` runs it as its own
# process.
#
# ## Module-level names
# - `PAGE` — absolute path of `console.html` beside this file; served at `/`.
#
# ### `__init__(self, ctl, marks_root=None, index=None, worst_failover=0.0, wall=None, extra=None,
# media=False, lost_after=45.0)` `ctl` is the subsystem's `SpecController` holding the console's token;
# `marks_root` is this server's resource root — if given, `self.marks` is an `EventLog(marks_root,
# "console", <hostname:pid>, epoch 1)` (the console's own log; one writer, so epoch 1 forever); `index` is
# an optional `MergedIndex` (anything with `query(...)`); `worst_failover` is a number exported on `/metrics`; `extra` is the subsystem's
# route function; `media` tells the page it may draw a timeline and play. `seen` is the `IdempotencyKeys`
# over `<sub>/idem/`. `_scan` caches the assignment directory; `scans` counts cache refreshes.
#
# ## Notes
# - `test_the_console_over_http` walks the whole surface: POST twice with one key is one camera; the
#   console's controller cannot `place` (`Forbidden`); PUT `{"worker": "w-9"}` is 400; `/cameras` rows show
#   `phase running` and `server srv-1`; `/where/1` agrees with the directory; `/spec` says `rows cameras,
#   media true`; `/metrics` contains `vms_cameras_recording 1`; `/marks` writes to `console/<instance>/e1/`;
#   the page mentions `/spec`, `/timeline/`, `/segment/`, `<video>` and never the word camera outside its
#   comment; `/timeline/1` and a ranged `/segment/` come from the VMS extra; PUT `{"enabled": false}` bumps
#   revision to 2; DELETE marks the row and the placement waits for `unplace_deleted`.
# - Idempotency covers POST always, PUT optionally, DELETE never; the page sends a fresh key with every
#   request (including DELETE, where it is ignored).
# - `/events` relies on a `MergedIndex` behind the console — every live resource's own event database, merged; without one it is an honest 503,
#   and the page tolerates that.
# ================================================================================================
from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .contract import Assignment, Heartbeat
from .epoch import current_epoch
from .events import EventLog
from .resource import resources_seen
from .spec import Refused, SpecController
from .variables import Conflict

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")


# A file, whole or by `Range` — what a `<video>` element asks for. Parses `bytes=a-b`, replies 206 with
# `Content-Range` and `Accept-Ranges` when a range was asked, 200 otherwise. Used by the VMS's
# `/segment/<path>` extra; the test asks `bytes=10-19` and gets 206 with `Content-Range: bytes 10-19/256`.
def send_file(handler, path: str, content_type: str) -> None:
    """A file, whole or by Range — what a <video> element asks for."""
    size = os.path.getsize(path); start, end = 0, size - 1
    rng = handler.headers.get("Range")
    if rng and rng.startswith("bytes="):
        a, b = rng[6:].split("-"); start = int(a or 0); end = int(b) if b else end
    with open(path, "rb") as f:
        f.seek(start); data = f.read(end - start + 1)
    handler.send_response(206 if rng else 200); handler.send_header("Content-Type", content_type)
    handler.send_header("Accept-Ranges", "bytes"); handler.send_header("Content-Length", str(len(data)))
    if rng:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers(); handler.wfile.write(data)


# Every worker's last heartbeat under `prefix`, whatever its age — the read model's and `/metrics`' source.
# Same scan as `Controller.workers_seen` without the age filter.
def heartbeats(objects, prefix: str) -> dict[str, Heartbeat]:
    """Every worker's last heartbeat, whatever its age — the read model's source."""
    out = {}
    for key in objects.list(prefix):
        if key.endswith("/heartbeat"):
            raw = objects.get(key)
            if raw:
                hb = Heartbeat.from_bytes(raw)
                out[hb.worker] = hb
    return out


# A retried POST must be the same POST whichever console answers it, so the key lives in the store, not in a
# process: `<sub>/idem/<key>` is claimed by a create-only CAS before the write and filled with the reply
# after it. A second instance that sees the claim waits for the reply and serves it; it never repeats the
# write. Keys older than `ttl` are pruned on the way past, at most once a minute.
# What every stdlib handler in the course needs: a JSON (or raw text) reply with the two headers, and the
# JSON body of a request. Mixed into the console's handler and the gateway's.
class SendMixin:
    def _send(self, status, body, raw=False):
        data = body.encode() if raw else json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "text/plain" if raw else "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")


class IdempotencyKeys:
    """A retried POST must be the same POST whichever console answers it, so
    the key lives in the store, not in a process: `<sub>/idem/<key>` is
    claimed by a create-only CAS before the write and filled with the reply
    after it. A second instance that sees the claim waits for the reply and
    serves it; it never repeats the write. Keys older than `ttl` are pruned
    on the way past, at most once a minute."""

    # `prefix` is `<sub>/idem/`; `wall` stamps `at`; `clock` rate-limits pruning; `sleep` is the wait
    # between polls (injected for tests).
    def __init__(self, vars_, prefix: str, wall, ttl: float = 86400.0, clock=time.monotonic, sleep=time.sleep):
        self.vars, self.prefix, self.wall, self.ttl, self.clock, self.sleep = vars_, prefix, wall, ttl, clock, sleep
        self._pruned = -1e9

    # Refuses an empty key, one containing `/` or `..`, or longer than 200 chars (`Refused`: must be one
    # path segment) and returns `prefix + key`.
    def _path(self, key: str) -> str:
        if not key or "/" in key or ".." in key or len(key) > 200:
            raise Refused("Idempotency-Key must be one path segment")
        return self.prefix + key

    # Try `put({state: pending, at}, cas=0)`: success means ours to answer — return `None` (the caller does
    # the write, then `store`). On `Conflict`, another instance holds it: poll up to 40 × 50 ms; if the row
    # vanished (pruned or the claimant crashed mid-flight) claim again; if `state == done` return `(status,
    # body)`; after the polls, `409 in flight`. `test_a_retry_that_lands_on_another_console_is_one_camera`
    # covers all of it: the second console returns the first's 201 body; a key with a pending claim makes
    # console A wait and then serve B's reply without creating a camera; a key `a/b` is 400.
    def claim(self, key: str):
        """None: ours to answer — do the write, then store(). Else the reply to serve."""
        path = self._path(key)
        try:
            self.vars.put(path, {"state": "pending", "at": self.wall()}, cas=0)   # create-only: the first claimant wins
            self.prune()
            return None
        except Conflict:
            pass
        for _ in range(40):                                              # another instance holds it: its reply, when it lands
            items, _ = self.vars.get(path)
            if items is None:
                return self.claim(key)                                   # pruned or crashed mid-flight: claim again
            if items.get("state") == "done":
                return int(items["status"]), json.loads(items["body"])
            self.sleep(0.05)
        return 409, {"detail": "the same request is in flight on another console", "error": "in flight"}

    # Overwrite the row with `{state: done, status, body: json, at}` (no CAS: the claimant owns it).
    def store(self, key: str, resp: tuple[int, dict]) -> None:
        self.vars.put(self._path(key), {"state": "done", "status": resp[0], "body": json.dumps(resp[1]), "at": self.wall()})

    # At most once per 60 s of monotonic time: delete every key under the prefix whose `at` is older than
    # `ttl`, by CAS (a conflict skips it). The test advances a day and sees 2 pruned, then 0, then a re-used
    # key create a new camera — a forgotten key is a new request, by design.
    def prune(self) -> int:
        if self.clock() - self._pruned < 60:
            return 0
        self._pruned = self.clock(); n = 0; now = self.wall()
        for path in self.vars.list(self.prefix):
            items, idx = self.vars.get(path)
            if items and now - float(items.get("at", 0)) > self.ttl:
                try:
                    self.vars.delete(path, cas=idx); n += 1
                except Conflict:
                    pass
        return n


# One console for every subsystem.
class SpecConsole:
    """One console for every subsystem. `ctl` is the subsystem's SpecController
    holding the console's token; `media` says the page may draw a timeline and
    play (the subsystem's `extra` serves /timeline and /segment)."""

    def __init__(self, ctl: SpecController, marks_root: str | None = None, index=None, worst_failover: float = 0.0,
                 wall=None, extra=None, media: bool = False, lost_after: float = 45.0):
        self.ctl, self.spec, self.index = ctl, ctl.spec, index
        self.worst_failover, self.wall, self.extra, self.media, self.lost_after = worst_failover, wall or ctl.wall, extra, media, lost_after
        self.instance = f"{socket.gethostname()}:{os.getpid()}"
        self.marks_root = marks_root
        self.marks = EventLog(marks_root, "console", self.instance, 1) if marks_root else None   # the console's own log: one writer, so epoch 1
        self.seen = IdempotencyKeys(ctl.vars, f"{self.spec.name}/idem/", self.wall)   # in the store: any instance answers a retry
        self._scan: tuple[float, dict] = (-1e9, {})
        self.scans = 0

    # -- what the page reads first ------------------------------------------------------------
    # `/spec`'s body: `{name, rows, id, media, fields: [{name, type, default, required}], metrics: {prefix,
    # running}}` — the page's only knowledge of the subsystem.
    def describe(self) -> dict:
        s = self.spec
        return {"name": s.name, "rows": s.rows, "id": s.id, "media": self.media,
                "fields": [{"name": f.name, "type": f.type, "default": f.default_value(), "required": f.required} for f in s.fields.values()],
                "metrics": {"prefix": s.name, "running": s.running_gauge}}

    # -- the directory: where is unit N, in one scan of the assignments ---------------------------
    # `{worker: units}` from one scan of the assignments, cached for 5 s of monotonic time.
    def directory(self) -> dict[str, list[str]]:
        now = time.monotonic()
        if now - self._scan[0] >= 5.0:
            self._scan = (now, {w: a.units for w, a in self.ctl.assignments().items()}); self.scans += 1
        return self._scan[1]

    # Every worker whose assignment lists the unit, joined with `+` — a reassignment window shows as both.
    # Compared against the placement row in `/where`.
    def where(self, uid) -> str | None:
        hits = sorted(w for w, units in self.directory().items() if str(uid) in units)
        return "+".join(hits) if hits else None                       # a reassignment window shows as both

    # -- metrics ------------------------------------------------------------------------------
    # Prometheus text, all prefixed with the subsystem name: `<p>_workers_live`;
    # `<p>_worker_headroom{worker,server}` per live worker and the total `<p>_headroom` (what the autoscaler
    # sums); `<p>_worker_load{worker}` = `1 − headroom/capacity` per live worker (assigned/capacity: what a
    # target-value policy scales on); `<p>_epoch_conflicts{worker}` counter from every heartbeat;
    # `<p>_failover_seconds{kind="worst"}`; `<p>_resources_live`; and `<p>_<running_gauge>` — the count of
    # status entries in phase `running` on live workers (`vms_cameras_recording`). The page reads two of
    # these for its status line.
    # The servers this subsystem runs on, as the placement sees them: every server a worker heartbeats from
    # or a resource heartbeats from — the archive root its workers say they record into (on a cluster the
    # value of Nomad's `meta.archive`, the label the scheduler placed by), the state of its resource (`live`,
    # `silent`, `unknown`), its workers with load and capacity, and whether the controller would place
    # there now, with the reason when it would not.
    def servers(self) -> dict:
        ctl, now = self.ctl, self.wall()
        out: dict[str, dict] = {}
        for w, hb in heartbeats(ctl.objects, ctl.sub.name + "/").items():
            s = out.setdefault(hb.extra.get("server", "?"), {"archive": None, "resource": "unknown", "workers": []})
            if hb.extra.get("archive"):
                s["archive"] = hb.extra["archive"]
            s["workers"].append({"worker": w, "load": ctl.load(w), "capacity": ctl.capacity_of(w), "labels": hb.extra.get("labels", ""),
                                 "state": "live" if now - hb.ts <= self.lost_after else "stale"})
        for server in resources_seen(ctl.objects):
            out.setdefault(server, {"archive": None, "resource": "unknown", "workers": []})
        for server, s in out.items():
            s["resource"] = ctl.resource_state(server, self.lost_after)
            s["requires_resource"] = ctl.spec.requires == "resource"
            s["placeable"] = not (s["requires_resource"] and s["resource"] == "silent")
            s["why"] = f"resource on {server} silent" if not s["placeable"] else None
            s["workers"].sort(key=lambda x: x["worker"])
        return dict(sorted(out.items()))

    def metrics_text(self) -> str:
        p = self.spec.name
        hbs = heartbeats(self.ctl.objects, p + "/"); now = self.wall()
        live = {w: hb for w, hb in hbs.items() if now - hb.ts <= self.lost_after}
        res = resources_seen(self.ctl.objects)
        lines = [f"# TYPE {p}_workers_live gauge", f"{p}_workers_live {len(live)}",
                 f"# TYPE {p}_worker_headroom gauge",
                 *[f'{p}_worker_headroom{{worker="{w}",server="{hb.extra.get("server", "?")}"}} {hb.extra.get(self.spec.headroom_from, 0)}' for w, hb in live.items()],
                 f"{p}_headroom {sum(int(hb.extra.get(self.spec.headroom_from, 0)) for hb in live.values())}",
                 f"# TYPE {p}_worker_load gauge",              # assigned / capacity: what a target-value policy scales on
                 *[f'{p}_worker_load{{worker="{w}"}} {1 - int(hb.extra.get(self.spec.headroom_from, 0)) / max(1, int(hb.extra.get(self.spec.capacity_from, 1))):.3f}' for w, hb in live.items()],
                 f"# TYPE {p}_epoch_conflicts counter",
                 *[f'{p}_epoch_conflicts{{worker="{w}"}} {hb.extra.get("conflicts", 0)}' for w, hb in hbs.items()],
                 f"# TYPE {p}_failover_seconds gauge", f'{p}_failover_seconds{{kind="worst"}} {self.worst_failover}',
                 f"# TYPE {p}_resources_live gauge", f"{p}_resources_live {sum(1 for hb in res.values() if now - float(hb['ts']) <= self.lost_after)}",
                 f"# TYPE {p}_{self.spec.running_gauge} gauge",
                 f"{p}_{self.spec.running_gauge} {sum(1 for hb in live.values() for s in hb.status if s.get('phase') == 'running')}"]
        return "\n".join(lines) + "\n"

    # -- writes ---------------------------------------------------------------------------------
    # `ctl.create(body)` → 201 with the row plus `worker: None` (placed by the controller's next pass, never
    # by the console); `Refused` → 400 `{detail, error}`.
    def create(self, body: dict) -> tuple[int, dict]:
        try:
            r = self.ctl.create(body)
            return 201, {**r, "worker": None}                        # placed by the controller's next pass, never by the console
        except Refused as e:
            return 400, {"detail": str(e), "error": str(e)}

    # `ctl.update` → 200 with the row; `Refused` → 400; `KeyError` → 404.
    def update(self, uid, body: dict) -> tuple[int, dict]:
        try:
            return 200, self.ctl.update(uid, body)
        except Refused as e:
            return 400, {"detail": str(e), "error": str(e)}
        except KeyError:
            return 404, {"detail": "no such unit", "error": "no such unit"}

    # 404 if the unit is absent; else `ctl.delete(uid)` and 200 `{deleted: uid}`.
    def delete(self, uid) -> tuple[int, dict]:
        if self.ctl.unit(uid) is None:
            return 404, {"detail": "no such unit", "error": "no such unit"}
        self.ctl.delete(uid)
        return 200, {"deleted": uid}

    # An operator's observation. 503 if there is no resource on this server; 400 unless the body names `cam`
    # or `unit`. Appends `{kind: mark, user, note, cam|unit}` at `wall()` to the console's own bucket —
    # `cam` is stored as an int because it is the field the index joins on — and returns 201 `{subsystem:
    # "console", unit: <instance>, bucket: <relative path>}`. It is the console's event, into
    # `console/<instance>/e1/…` on this server's resource, never a worker's bucket.
    def mark(self, body: dict, user: str) -> tuple[int, dict]:
        if self.marks is None:
            return 503, {"detail": "no resource on this server to write marks into", "error": "no resource on this server to write marks into"}
        if "cam" not in body and "unit" not in body:
            return 400, {"detail": "a mark names a unit", "error": "a mark names a unit"}
        fields = {"user": user, "note": str(body.get("note", ""))}
        if "cam" in body:
            fields["cam"] = int(body["cam"])                          # the field the index joins on
        else:
            fields["unit"] = str(body["unit"])
        path = self.marks.append(self.wall(), "mark", **fields)
        return 201, {"subsystem": "console", "unit": self.instance, "bucket": os.path.relpath(path, self.marks_root)}

    # -- the handler ----------------------------------------------------------------------------
    # Builds and returns the request handler class bound to this console.
    #
    # #### `class H(BaseHTTPRequestHandler)` (nested)
    # - `log_message` — silenced.
    # - `_send(status, body, raw=False)` — JSON (or raw text) with `Content-Type` and `Content-Length`.
    # - `_body()` — the JSON request body, `{}` if empty.
    # - `_uid()` — the last path segment (query stripped) through `spec.parse_id`.
    # - `_extra(method, path, q) -> bool` — call the subsystem's `extra`; `None` means not ours (return
    #   False). `()` means the extra wrote the reply itself (`send_file`). `(status, dict|list)` is sent as
    #   JSON; `(status, bytes)` raw; `(status, bytes, headers)` raw with headers.
    # - `do_GET` — routes, in order:
    #   - `GET /` or `/index.html` — `console.html`.
    #   - `GET /spec` — `describe()`.
    #         - `GET /<rows>` — `{rows: ctl.read_model(lost_after), configured: ctl.units()}`: the
    #       heartbeats' view over the configured rows.
    #         - `GET /where/<id>` — `{worker, reason}` from the stored placement (404 with nulls if
    #       unplaced), plus `directory` (the assignments' answer) and `scans`.
    #   - `GET /resources` — every resource heartbeat with `state: live | silent` by `lost_after`.
    #   - `GET /servers` — `servers()`: per server, `archive` (what its workers record into — Nomad's `meta.archive`
    #     on a cluster), `resource` (`live | silent | unknown`), `workers`, `placeable` and `why`.
    #   - `GET /unplaceable` — `ctl.unplaceable()`.
    #         - `GET /events?from&to&cam|unit&kind&subsystem` — 503 if no index; else builds
    #       `current_epochs` from every `<sub>/epoch/*` row and calls `index.query`. A numeric `unit` is
    #       treated as `cam`; a non-numeric one is passed as `unit`.
    #   - `GET /metrics` — `metrics_text()` as `text/plain`.
    #   - otherwise `_extra("GET", …)`; then 404 `{detail, error}`.
    # - `_idem() -> key | None` — for POST: 400 if `Idempotency-Key` is missing or malformed; if `claim`
    #   returns a prior reply, send it and return `None`; else return the key.
    # - `do_POST`:
    #     - `POST /<rows>` (Idempotency-Key required) — `create(body)`; the reply is stored under the key
    #     and sent.
    #         - `POST /marks` (Idempotency-Key required; `X-User` header, default `operator`) — `mark(body,
    #       user)`; stored and sent.
    #   - any other path — `_extra("POST", …)` or 404.
    # - `do_PUT`:
    #         - `PUT /<rows>/<id>` — `update(uid, body)`; `Idempotency-Key` is optional here: if present the
    #       same claim/store dance applies. Any other path is 404.
    # - `do_DELETE`:
    #         - `DELETE /<rows>/<id>` — `delete(uid)`; no idempotency key (a second delete is 404, "gone is
    #       gone"). Any other path is 404.
    def handler(self):
        """The request handler class for this console alone — a Mount with no other subsystems."""
        return Mount(self).handler()

    # -- one request, already stripped of any mount prefix: the routes above -----------------------------
    def _uid(self, path):
        return self.spec.parse_id(path.rsplit("/", 1)[1])

    def _extra(self, h, method, path, q):
        r = self.extra(h, method, path, q) if self.extra else None
        if r is None:
            return False
        if r == ():                                                  # the extra wrote the reply itself (send_file)
            return True
        if len(r) == 2 and isinstance(r[1], (dict, list)):
            h._send(*r)
        elif len(r) == 2:
            h.send_response(r[0]); h.send_header("Content-Length", str(len(r[1]))); h.end_headers(); h.wfile.write(r[1])
        else:
            status, data, headers = r
            h.send_response(status); h.send_header("Content-Length", str(len(data)))
            for k, v in headers: h.send_header(k, v)
            h.end_headers(); h.wfile.write(data)
        return True

    def _idem(self, h):
        key = h.headers.get("Idempotency-Key")
        if not key:
            h._send(400, {"detail": "Idempotency-Key header is required", "error": "Idempotency-Key required"}); return None
        try:
            prior = self.seen.claim(key)
        except Refused as e:
            h._send(400, {"detail": str(e), "error": str(e)}); return None
        if prior is not None:
            h._send(*prior); return None
        return key

    def dispatch(self, h, method: str, path: str, q: dict) -> None:
        """Answer one request for this subsystem. `path` is the route (`/<rows>`, `/where/7`), the mount
        prefix already removed; `h` is the handler (its `_send`, `_body`, `headers`, `rfile`)."""
        con, ctl, spec = self, self.ctl, self.spec
        rows_path = "/" + spec.rows
        if method == "GET":
            if path in ("/", "/index.html"):
                return send_file(h, PAGE, "text/html; charset=utf-8")
            if path == "/spec":
                return h._send(200, con.describe())
            if path == rows_path:
                return h._send(200, {"rows": ctl.read_model(con.lost_after), "configured": ctl.units()})
            if path.startswith("/where/"):
                uid = self._uid(path); pl = ctl.placement(uid)
                return h._send(200 if pl else 404, {"worker": pl.worker if pl else None, "reason": pl.reason if pl else None,
                                                    "directory": con.where(uid), "scans": con.scans})
            if path == "/resources":
                now = con.wall()
                return h._send(200, {s: {**hb, "state": "live" if now - float(hb["ts"]) <= con.lost_after else "silent"}
                                     for s, hb in resources_seen(ctl.objects).items()})
            if path == "/servers":
                return h._send(200, con.servers())
            if path == "/unplaceable":
                return h._send(200, ctl.unplaceable())
            if path == "/events":
                if con.index is None:
                    return h._send(503, {"error": "no event database behind this console"})
                cur = {(p.split("/")[0], p.rsplit("/", 1)[1]): current_epoch(ctl.vars, p) for p in ctl.vars.list("") if "/epoch/" in p}   # every subsystem's epochs: the timeline shows them all
                cam = q.get("cam") or (q.get("unit") if (q.get("unit") or "").isdigit() else None)
                return h._send(200, con.index.query(float(q.get("from", 0)), float(q.get("to", 1e12)),
                                                    int(cam) if cam else None, q.get("kind"), q.get("subsystem"),
                                                    q.get("unit") if not cam else None, cur))
            if path == "/metrics":
                return h._send(200, con.metrics_text(), raw=True)
            if self._extra(h, "GET", path, q):
                return
            return h._send(404, {"detail": "no such route", "error": "no such path"})
        if method == "POST":
            if path not in (rows_path, "/marks"):
                if self._extra(h, "POST", path, q):
                    return
                return h._send(404, {"detail": "no such route", "error": "no such path"})
            key = self._idem(h)
            if key is None:
                return
            if path == "/marks":
                resp = con.mark(h._body(), h.headers.get("X-User", "operator"))
            else:
                resp = con.create(h._body())
            con.seen.store(key, resp); return h._send(*resp)
        if method == "PUT":
            if not path.startswith(rows_path + "/"):
                if self._extra(h, "PUT", path, q):
                    return
                return h._send(404, {"detail": "no such route", "error": "no such path"})
            key = h.headers.get("Idempotency-Key")
            if key:
                try:
                    prior = con.seen.claim(key)
                except Refused as e:
                    return h._send(400, {"detail": str(e), "error": str(e)})
                if prior is not None:
                    return h._send(*prior)
            resp = con.update(self._uid(path), h._body())
            if key:
                con.seen.store(key, resp)
            return h._send(*resp)
        if method == "DELETE":
            if not path.startswith(rows_path + "/"):
                if self._extra(h, "DELETE", path, q):
                    return
                return h._send(404, {"detail": "no such route", "error": "no such path"})
            return h._send(*con.delete(self._uid(path)))
        h._send(405, {"detail": "method", "error": "method"})

    # Starts the server in a daemon thread and returns it (tests use `port=0` and read `server_address`).
    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((host, port), self.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


class Mount:
    """One console process, several subsystems. The root console answers at `/`
    (the page, `/<rows>`, its extras); every other subsystem is a path: `/live/spec`,
    `/det/units`, `/det/where/7-motion` — the same SpecConsole class, its routes
    under its name, its own token-scoped controller. A person opens one page;
    the machines (the autoscaler, М12's read model) find every subsystem on one
    port; a new subsystem is a YAML, a worker, and a path."""

    def __init__(self, root: SpecConsole, mounts: dict[str, SpecConsole] | None = None):
        self.root, self.mounts = root, dict(mounts or {})

    def mount(self, name: str, console: SpecConsole) -> "Mount":
        self.mounts[name] = console
        return self

    def resolve(self, path: str) -> tuple[SpecConsole, str]:
        head = path.split("/", 2)
        if len(head) >= 2 and head[1] in self.mounts:
            return self.mounts[head[1]], "/" + (head[2] if len(head) > 2 else "")
        return self.root, path

    def describe(self) -> dict:
        return {"root": self.root.spec.name, "mounts": {n: c.describe() for n, c in self.mounts.items()}}

    def handler(self):
        mnt = self

        class H(SendMixin, BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _route(self, method):
                u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == "/mounts":
                    return self._send(200, mnt.describe())
                con, path = mnt.resolve(u.path)
                con.dispatch(self, method, path, q)

            def do_GET(self): self._route("GET")
            def do_POST(self): self._route("POST")
            def do_PUT(self): self._route("PUT")
            def do_DELETE(self): self._route("DELETE")

        return H

    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((host, port), self.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
