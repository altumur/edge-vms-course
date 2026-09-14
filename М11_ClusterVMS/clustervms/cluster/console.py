"""The cluster console, standard library — its own job (count ≥ 2, anywhere),
its own token (the operator's rows, never placement). М10's console with
three more reads and no more writes:

    GET /                  М10's page, unchanged: the camera list, a timeline merged across resources, playback
    GET /segment/<path>?server=<s>   the bytes of one segment, fetched from THAT server's resource job (Range passed through)
    GET /cameras           the read model from every worker's heartbeat, with server and age
    GET /where/<id>        the stored placement (why), and the directory's answer (where, one scan)
    GET /timeline/<id>     merged across the resources that hold the camera; unreachable ones named
    GET /resources         which archive resources exist, their usage, which are silent
    GET /unplaceable       cameras nothing live can reach, with the labels that say why
    GET /events?from&to&cam&kind&subsystem&unit   from the eventindex — a cache over the resources; its state says if it is catching up
    GET /metrics           vms_workers_live, vms_worker_headroom, vms_worker_load, vms_epoch_conflicts,
                           vms_failover_seconds{kind="worst"}, vms_resources_live, vms_cameras_recording
    POST /cameras, PUT /cameras/<id>, DELETE /cameras/<id>     through the controller; Idempotency-Key on the POST
    POST /marks {cam, note}              an operator's observation: the console's own bucket, console/<instance>/…, on
                                         THIS server's resource — never a worker's bucket; the eventindex joins on `cam`
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import urllib.error
import urllib.request

from vms.console import PAGE, send_file
from vms.controller import Refused
from vmsplatform.epoch import current_epoch
from vmsplatform.events import EventLog

from .controller import ClusterController, heartbeats
from .directory import Directory
from vmsplatform.resource import resources_seen
from .timeline import ManifestReader, merged_timeline


def metrics_text(ctl: ClusterController, worst_failover: float) -> str:
    hbs = heartbeats(ctl.objects)
    now = ctl.wall()
    live = {w: hb for w, hb in hbs.items() if now - hb.ts <= 45}
    res = resources_seen(ctl.objects)
    lines = ["# TYPE vms_workers_live gauge", f"vms_workers_live {len(live)}",
             "# TYPE vms_worker_headroom gauge",
             *[f'vms_worker_headroom{{worker="{w}",server="{hb.extra.get("server", "?")}"}} {hb.extra.get("headroom", 0)}' for w, hb in live.items()],
             f"vms_headroom {sum(int(hb.extra.get('headroom', 0)) for hb in live.values())}",
             "# TYPE vms_worker_load gauge",
             *[f'vms_worker_load{{worker="{w}"}} {1 - int(hb.extra.get("headroom", 0)) / max(1, int(hb.extra.get("capacity", 1))):.3f}' for w, hb in live.items()],
             "# TYPE vms_epoch_conflicts counter",
             *[f'vms_epoch_conflicts{{worker="{w}"}} {hb.extra.get("conflicts", 0)}' for w, hb in hbs.items()],
             "# TYPE vms_failover_seconds gauge", f'vms_failover_seconds{{kind="worst"}} {worst_failover}',
             "# TYPE vms_resources_live gauge",
             f"vms_resources_live {sum(1 for hb in res.values() if now - float(hb['ts']) <= 45)}",
             "# TYPE vms_cameras_recording gauge",
             f"vms_cameras_recording {sum(1 for hb in live.values() for s in hb.status if s['phase'] == 'running')}"]
    return "\n".join(lines) + "\n"


def make_handler(ctl: ClusterController, reader=None, worst_failover: float = 0.0, index=None, archive_root: str | None = None):
    reader = reader or ManifestReader()
    directory = Directory(ctl.vars, ttl=5.0)
    seen: dict[str, tuple[int, dict]] = {}
    instance = f"{socket.gethostname()}:{os.getpid()}"
    marks = EventLog(archive_root, "console", instance, 1) if archive_root else None

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, status, body, raw=False):
            data = body.encode() if raw else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain" if raw else "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                return send_file(self, PAGE, "text/html; charset=utf-8")
            if u.path.startswith("/segment/"):
                rel = u.path[len("/segment/"):]; res = resources_seen(ctl.objects).get(q.get("server", ""))
                if ".." in rel or res is None:
                    return self._send(404, {"error": "no such resource"})
                req = urllib.request.Request(f"{res['url']}/segment/{rel}", headers={k: v for k, v in (("Range", self.headers.get("Range")),) if v})
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:
                        data = r.read(); status = r.status; crange = r.headers.get("Content-Range")
                except urllib.error.HTTPError as e:
                    return self._send(e.code, {"error": f"the resource on {q['server']} said {e.code}"})
                except OSError:
                    return self._send(503, {"error": f"the resource on {q['server']} is not answering — unavailable, not lost"})
                self.send_response(status); self.send_header("Content-Type", "video/mp4"); self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(data)))
                if crange: self.send_header("Content-Range", crange)
                self.end_headers(); self.wfile.write(data); return
            if u.path == "/cameras":
                return self._send(200, {"rows": ctl.read_model(), "configured": ctl.cameras()})
            if u.path.startswith("/where/"):
                cid = int(u.path.rsplit("/", 1)[1]); pl = ctl.placement(cid)
                return self._send(200 if pl else 404, {"worker": pl.worker if pl else None, "reason": pl.reason if pl else None,
                                                       "directory": directory.where(cid), "scans": directory.scans})
            if u.path.startswith("/timeline/"):
                cid = int(u.path.rsplit("/", 1)[1])
                cur = current_epoch(ctl.vars, ctl.sub.epoch_key(str(cid))) or None
                return self._send(200, merged_timeline(resources_seen(ctl.objects), reader, cid,
                                                       float(q.get("from", 0)), float(q.get("to", 1e12)), cur, ctl.wall()))
            if u.path == "/resources":
                now = ctl.wall()
                return self._send(200, {s: {**hb, "state": "live" if now - float(hb["ts"]) <= 45 else "silent"}
                                        for s, hb in resources_seen(ctl.objects).items()})
            if u.path == "/unplaceable":
                return self._send(200, ctl.unplaceable())
            if u.path == "/events":
                if index is None:
                    return self._send(503, {"error": "no eventindex in this cluster"})
                cur = {("vms", p.rsplit("/", 1)[1]): current_epoch(ctl.vars, p) for p in ctl.vars.list(ctl.sub.name + "/epoch/")}
                return self._send(200, index.query(float(q.get("from", 0)), float(q.get("to", 1e12)),
                                                   int(q["cam"]) if "cam" in q else None, q.get("kind"),
                                                   q.get("subsystem"), q.get("unit"), cur))
            if u.path == "/metrics":
                return self._send(200, metrics_text(ctl, worst_failover), raw=True)
            self._send(404, {"error": "no such path"})

        def do_POST(self):
            if self.path not in ("/cameras", "/marks"):
                return self._send(404, {})
            key = self.headers.get("Idempotency-Key")
            if not key:
                return self._send(400, {"error": "Idempotency-Key required"})
            if key in seen:
                return self._send(*seen[key])
            if self.path == "/marks":
                b = self._body()
                if marks is None:
                    seen[key] = (503, {"error": "no resource on this server to write marks into"})
                elif "cam" not in b:
                    seen[key] = (400, {"error": "a mark names a camera"})
                else:
                    p = marks.append(ctl.wall(), "mark", cam=int(b["cam"]), user=self.headers.get("X-User", "operator"), note=str(b.get("note", "")))
                    seen[key] = (201, {"subsystem": "console", "unit": instance, "bucket": os.path.relpath(p, archive_root)})
                return self._send(*seen[key])
            try:
                r = ctl.create_camera(self._body())
                seen[key] = (201, {**r, "worker": None})        # placed by the controller's next pass, never by the console
            except Refused as e:
                seen[key] = (400, {"error": str(e)})
            self._send(*seen[key])

        def do_PUT(self):
            if not self.path.startswith("/cameras/"):
                return self._send(404, {})
            try:
                self._send(200, ctl.update_camera(int(self.path.rsplit("/", 1)[1]), self._body()))
            except Refused as e:
                self._send(400, {"error": str(e)})
            except KeyError:
                self._send(404, {"error": "no such camera"})

        def do_DELETE(self):
            if not self.path.startswith("/cameras/"):
                return self._send(404, {})
            cid = int(self.path.rsplit("/", 1)[1])
            if ctl.camera(cid) is None:
                return self._send(404, {"error": "no such camera"})
            ctl.delete_camera(cid)
            self._send(200, {"deleted": cid})

    return H


def serve(ctl, host="127.0.0.1", port=8080, reader=None, worst_failover=0.0, index=None, archive_root=None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(ctl, reader, worst_failover, index, archive_root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
