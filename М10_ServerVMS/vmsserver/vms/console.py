"""The one-box console, standard library. Its own process (`python3 -m vms
console`), with its own token: it writes the operator's rows — cameras,
next_id, retention — and never placement. Reads never touch a worker.

    GET  /                        the page: the camera list, a camera's timeline, playback of a span (console.html)
    GET  /segment/<path>          the bytes of one promoted segment from this box's archive, Range honoured
    GET  /cameras                 the read model: every camera from the workers' heartbeats, with age
    GET  /where/<id>              which worker — from the stored placement
    GET  /timeline/<id>?from&to   segments from the archive resource's manifest, fenced ones marked
    POST /cameras                 create (Idempotency-Key required) — the row only; the controller places it on its next pass
    POST /marks                   an operator's observation {cam, note} — the CONSOLE's event, into console/<instance>/…
                                  on this box's resource (never a worker's bucket; the index joins on `cam`)
    PUT  /cameras/<id>            update — refuses placement and controller-owned fields
    DELETE /cameras/<id>          the row is marked deleted; its assignment goes; footage stays until retention
    GET  /metrics                 vms_epoch_conflicts, vms_workers_live, vms_worker_headroom (the autoscaler's), vms_cameras_recording
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import os
import socket

from vmsplatform.events import EventLog

from .archive import ArchiveResource, Manifest
from .controller import Refused, VmsController


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


def make_handler(ctl: VmsController, archive: ArchiveResource | None, wall=None):
    seen: dict[str, tuple[int, dict]] = {}
    instance = f"{socket.gethostname()}:{os.getpid()}"
    marks = EventLog(archive.root, "console", instance, 1) if archive else None    # the console's own log: one writer, so epoch 1
    wall = wall or ctl.wall

    class H(BaseHTTPRequestHandler):
        def _send(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    return send_file(self, PAGE, "text/html; charset=utf-8")
                if u.path.startswith("/segment/") and archive:
                    rel = u.path[len("/segment/"):]; p = os.path.join(archive.root, rel)
                    if ".." in rel or not os.path.isfile(p):
                        return self._send(404, {"detail": "no such segment"})
                    return send_file(self, p, "video/mp4")
                if u.path == "/cameras":
                    return self._send(200, {"rows": ctl.read_model(), "configured": ctl.cameras()})
                if u.path.startswith("/where/"):
                    w = ctl.where(int(u.path.rsplit("/", 1)[1]))
                    return self._send(200 if w else 404, {"worker": w})
                if u.path.startswith("/timeline/") and archive:
                    cid = int(u.path.rsplit("/", 1)[1])
                    return self._send(200, Manifest(archive.root, cid).timeline(float(q.get("from", 0)), float(q.get("to", 1e12))))
                if u.path == "/metrics":
                    hbs = ctl.workers_seen()
                    lines = ["# TYPE vms_epoch_conflicts counter",
                             *[f'vms_epoch_conflicts{{worker="{w}"}} {hb.extra.get("conflicts", 0)}' for w, hb in hbs.items()],
                             "# TYPE vms_workers_live gauge", f"vms_workers_live {len(hbs)}",
                             "# TYPE vms_worker_headroom gauge",
                             *[f'vms_worker_headroom{{worker="{w}"}} {hb.extra.get("headroom", 0)}' for w, hb in hbs.items()],
                             f"vms_headroom {sum(int(hb.extra.get('headroom', 0)) for hb in hbs.values())}",
                             "# TYPE vms_worker_load gauge",              # assigned / capacity: what a target-value policy scales on
                             *[f'vms_worker_load{{worker="{w}"}} {1 - int(hb.extra.get("headroom", 0)) / max(1, int(hb.extra.get("capacity", 1))):.3f}' for w, hb in hbs.items()],
                             "# TYPE vms_cameras_recording gauge",
                             f"vms_cameras_recording {sum(1 for hb in hbs.values() for s in hb.status if s['phase'] == 'running')}"]
                    raw = ("\n".join(lines) + "\n").encode()
                    self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(raw)))
                    self.end_headers(); self.wfile.write(raw); return
                self._send(404, {"detail": "no such route"})
            except Exception as e:                     # noqa: BLE001
                self._send(500, {"detail": str(e)})

        def _idem(self):
            key = self.headers.get("Idempotency-Key")
            if not key:
                self._send(400, {"detail": "Idempotency-Key header is required"}); return None
            if key in seen:
                self._send(*seen[key]); return None
            return key

        def do_POST(self):
            if self.path == "/marks":
                key = self._idem()
                if key is None:
                    return
                if marks is None:
                    resp = (503, {"detail": "no resource on this box to write marks into"})
                else:
                    b = self._body(); user = self.headers.get("X-User", "operator")
                    if "cam" not in b:
                        resp = (400, {"detail": "a mark names a camera"})
                    else:
                        path = marks.append(wall(), "mark", cam=int(b["cam"]), user=user, note=str(b.get("note", "")))
                        resp = (201, {"subsystem": "console", "unit": instance, "bucket": os.path.relpath(path, archive.root)})
                seen[key] = resp; return self._send(*resp)
            if self.path != "/cameras":
                return self._send(404, {"detail": "no such route"})
            key = self._idem()
            if key is None:
                return
            try:
                r = ctl.create_camera(self._body())
                resp = (201, {**r, "worker": None})              # placed by the controller's next pass, never by the console
            except Refused as e:
                resp = (400, {"detail": str(e)})
            seen[key] = resp; self._send(*resp)

        def do_PUT(self):
            if not self.path.startswith("/cameras/"):
                return self._send(404, {"detail": "no such route"})
            key = self._idem()
            if key is None:
                return
            try:
                resp = (200, ctl.update_camera(int(self.path.rsplit("/", 1)[1]), self._body()))
            except Refused as e:
                resp = (400, {"detail": str(e)})
            except KeyError:
                resp = (404, {"detail": "no such camera"})
            seen[key] = resp; self._send(*resp)

        def do_DELETE(self):
            if not self.path.startswith("/cameras/"):
                return self._send(404, {"detail": "no such route"})
            cid = int(self.path.rsplit("/", 1)[1])
            if ctl.camera(cid) is None:
                return self._send(404, {"detail": "no such camera"})
            ctl.delete_camera(cid)
            self._send(200, {"deleted": cid})

        def log_message(self, *a):
            pass

    return H


def serve(ctl: VmsController, archive: ArchiveResource | None, host: str = "127.0.0.1", port: int = 8080, wall=None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(ctl, archive, wall))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
