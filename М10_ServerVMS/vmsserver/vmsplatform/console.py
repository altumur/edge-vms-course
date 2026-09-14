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
    GET  /events?from&to&unit&kind&subsystem   from the eventindex, if this console runs one
    GET  /metrics                <name>_workers_live · <name>_worker_headroom{worker,server} · <name>_worker_load ·
                                 <name>_epoch_conflicts · <name>_failover_seconds{kind="worst"} · <name>_resources_live ·
                                 <name>_<running> (units in phase "running"; the spec names the gauge)
    POST /<rows>  (Idempotency-Key)   the row only — the controller places it on its next pass
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

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")


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
        self.seen: dict[str, tuple] = {}
        self._scan: tuple[float, dict] = (-1e9, {})
        self.scans = 0

    # -- what the page reads first ------------------------------------------------------------
    def describe(self) -> dict:
        s = self.spec
        return {"name": s.name, "rows": s.rows, "id": s.id, "media": self.media,
                "fields": [{"name": f.name, "type": f.type, "default": f.default_value(), "required": f.required} for f in s.fields.values()],
                "metrics": {"prefix": s.name, "running": s.running_gauge}}

    # -- the directory: where is unit N, in one scan of the assignments ---------------------------
    def directory(self) -> dict[str, list[str]]:
        now = time.monotonic()
        if now - self._scan[0] >= 5.0:
            self._scan = (now, {w: a.units for w, a in self.ctl.assignments().items()}); self.scans += 1
        return self._scan[1]

    def where(self, uid) -> str | None:
        hits = sorted(w for w, units in self.directory().items() if str(uid) in units)
        return "+".join(hits) if hits else None                       # a reassignment window shows as both

    # -- metrics ------------------------------------------------------------------------------
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
    def create(self, body: dict) -> tuple[int, dict]:
        try:
            r = self.ctl.create(body)
            return 201, {**r, "worker": None}                        # placed by the controller's next pass, never by the console
        except Refused as e:
            return 400, {"detail": str(e), "error": str(e)}

    def update(self, uid, body: dict) -> tuple[int, dict]:
        try:
            return 200, self.ctl.update(uid, body)
        except Refused as e:
            return 400, {"detail": str(e), "error": str(e)}
        except KeyError:
            return 404, {"detail": "no such unit", "error": "no such unit"}

    def delete(self, uid) -> tuple[int, dict]:
        if self.ctl.unit(uid) is None:
            return 404, {"detail": "no such unit", "error": "no such unit"}
        self.ctl.delete(uid)
        return 200, {"deleted": uid}

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
    def handler(self):
        con, ctl, spec = self, self.ctl, self.spec
        rows_path = "/" + spec.rows

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, status, body, raw=False):
                data = body.encode() if raw else json.dumps(body).encode()
                self.send_response(status); self.send_header("Content-Type", "text/plain" if raw else "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(n) or b"{}")

            def _uid(self):
                return spec.parse_id(self.path.split("?")[0].rsplit("/", 1)[1])

            def _extra(self, method, path, q):
                r = con.extra(self, method, path, q) if con.extra else None
                if r is None:
                    return False
                if r == ():                                              # the extra wrote the reply itself (send_file)
                    return True
                if len(r) == 2 and isinstance(r[1], (dict, list)):
                    self._send(*r)
                elif len(r) == 2:
                    self.send_response(r[0]); self.send_header("Content-Length", str(len(r[1]))); self.end_headers(); self.wfile.write(r[1])
                else:
                    status, data, headers = r
                    self.send_response(status); self.send_header("Content-Length", str(len(data)))
                    for k, v in headers: self.send_header(k, v)
                    self.end_headers(); self.wfile.write(data)
                return True

            def do_GET(self):
                u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path in ("/", "/index.html"):
                    return send_file(self, PAGE, "text/html; charset=utf-8")
                if u.path == "/spec":
                    return self._send(200, con.describe())
                if u.path == rows_path:
                    return self._send(200, {"rows": ctl.read_model(con.lost_after), "configured": ctl.units()})
                if u.path.startswith("/where/"):
                    uid = self._uid(); pl = ctl.placement(uid)
                    return self._send(200 if pl else 404, {"worker": pl.worker if pl else None, "reason": pl.reason if pl else None,
                                                           "directory": con.where(uid), "scans": con.scans})
                if u.path == "/resources":
                    now = con.wall()
                    return self._send(200, {s: {**hb, "state": "live" if now - float(hb["ts"]) <= con.lost_after else "silent"}
                                            for s, hb in resources_seen(ctl.objects).items()})
                if u.path == "/unplaceable":
                    return self._send(200, ctl.unplaceable())
                if u.path == "/events":
                    if con.index is None:
                        return self._send(503, {"error": "no eventindex behind this console"})
                    cur = {(spec.name, p.rsplit("/", 1)[1]): current_epoch(ctl.vars, p) for p in ctl.vars.list(spec.name + "/epoch/")}
                    cam = q.get("cam") or (q.get("unit") if (q.get("unit") or "").isdigit() else None)
                    return self._send(200, con.index.query(float(q.get("from", 0)), float(q.get("to", 1e12)),
                                                           int(cam) if cam else None, q.get("kind"), q.get("subsystem"),
                                                           q.get("unit") if not cam else None, cur))
                if u.path == "/metrics":
                    return self._send(200, con.metrics_text(), raw=True)
                if self._extra("GET", u.path, q):
                    return
                self._send(404, {"detail": "no such route", "error": "no such path"})

            def _idem(self):
                key = self.headers.get("Idempotency-Key")
                if not key:
                    self._send(400, {"detail": "Idempotency-Key header is required", "error": "Idempotency-Key required"}); return None
                if key in con.seen:
                    self._send(*con.seen[key]); return None
                return key

            def do_POST(self):
                u = urlsplit(self.path)
                if u.path not in (rows_path, "/marks"):
                    if self._extra("POST", u.path, {}):
                        return
                    return self._send(404, {"detail": "no such route", "error": "no such path"})
                key = self._idem()
                if key is None:
                    return
                if u.path == "/marks":
                    resp = con.mark(self._body(), self.headers.get("X-User", "operator"))
                else:
                    resp = con.create(self._body())
                con.seen[key] = resp; self._send(*resp)

            def do_PUT(self):
                u = urlsplit(self.path)
                if not u.path.startswith(rows_path + "/"):
                    return self._send(404, {"detail": "no such route", "error": "no such path"})
                key = self.headers.get("Idempotency-Key")
                if key and key in con.seen:
                    return self._send(*con.seen[key])
                resp = con.update(self._uid(), self._body())
                if key:
                    con.seen[key] = resp
                self._send(*resp)

            def do_DELETE(self):
                u = urlsplit(self.path)
                if not u.path.startswith(rows_path + "/"):
                    return self._send(404, {"detail": "no such route", "error": "no such path"})
                self._send(*con.delete(self._uid()))

        return H

    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((host, port), self.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv
