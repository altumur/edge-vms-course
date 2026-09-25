"""The cluster console — the process that serves browsers, in the standard
library. One of the two cluster-level jobs (the other is the live gateway);
the domain cluster runs the same one pointed at every cluster.

    GET  /api/cameras?q=&page=&size=&cluster=   the read model, with each row's age and its cluster's state
    GET  /api/causes                            silence grouped by failure domain: one server, one cause
    GET  /api/where/<camera>                    the directory of directories, incompleteness included
    PUT  /api/cameras/<camera>                  proxied to the owning cluster's console; Idempotency-Key required;
                                                refuses placement fields
    GET  /healthz

Stateless: kill it, start another, the first pass rebuilds everything.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .api import ApiError, ConsoleAPI
from .federation import DomainDirectory
from .readview import ReadView


class Console:
    def __init__(self, directory: DomainDirectory, view: ReadView, api: ConsoleAPI, refresh_interval: float = 5.0):
        self.directory, self.view, self.api, self.refresh_interval = directory, view, api, refresh_interval
        self._stop = threading.Event()

    def _refresher(self):
        while not self._stop.is_set():
            try:
                self.view.refresh()
            except Exception:                              # noqa: BLE001 — a bad pass is a stale view, not a dead console
                pass
            self._stop.wait(self.refresh_interval)

    def handler(self):
        console = self

        class H(BaseHTTPRequestHandler):
            def _send(self, status: int, body: dict | list):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _token(self):
                auth = self.headers.get("Authorization", "")
                return auth[7:] if auth.startswith("Bearer ") else None

            def do_GET(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    if u.path == "/healthz":
                        return self._send(200, {"ok": True, "passes": console.view.passes})
                    if u.path == "/api/cameras":
                        return self._send(200, console.view.list(q.get("q", ""), int(q.get("page", 1)),
                                                                 int(q.get("size", 50)), q.get("cluster")))
                    if u.path == "/api/causes":
                        return self._send(200, [c.__dict__ | {"sentence": c.sentence()} for c in console.view.causes()])
                    if u.path.startswith("/api/where/"):
                        a = console.directory.where(u.path.rsplit("/", 1)[1])
                        return self._send(200 if a.found else (404 if a.complete else 503),
                                          a.__dict__ | {"complete": a.complete, "sentence": a.sentence()})
                    self._send(404, {"detail": "no such route"})
                except Exception as e:                     # noqa: BLE001
                    self._send(500, {"detail": str(e)})

            def do_PUT(self):
                u = urlsplit(self.path)
                if not u.path.startswith("/api/cameras/"):
                    return self._send(404, {"detail": "no such route"})
                key = self.headers.get("Idempotency-Key")
                if not key:
                    return self._send(400, {"detail": "Idempotency-Key header is required: a retried PUT must be the same PUT"})
                n = int(self.headers.get("Content-Length", 0))
                fields = json.loads(self.rfile.read(n) or b"{}")
                try:
                    resp = console.api.update_camera(u.path.rsplit("/", 1)[1], fields, key, self._token())
                    self._send(202 if resp.get("pending") else 200, resp)    # kept for a cluster that is off: accepted, not applied
                except ApiError as e:
                    self._send(e.status, {"detail": e.detail})

            def log_message(self, *a):
                pass

        return H

    def serve(self, host: str = "127.0.0.1", port: int = 8090) -> ThreadingHTTPServer:
        threading.Thread(target=self._refresher, daemon=True, name="readview").start()
        srv = ThreadingHTTPServer((host, port), self.handler())
        threading.Thread(target=srv.serve_forever, daemon=True, name="console").start()
        return srv

    def stop(self, srv: ThreadingHTTPServer) -> None:
        self._stop.set()
        srv.shutdown()
        srv.server_close()


def main() -> None:
    """python3 -m domain.console — the cluster (or domain) console."""
    import os
    import signal
    import threading

    from .runtime import federation_from_env
    from .tokens import verify
    from .agent import ClusterTrust

    fed = federation_from_env()
    directory = DomainDirectory(fed)
    view = ReadView(fed, lost_after=float(os.environ.get("LOST_AFTER", "45")))
    trust = ClusterTrust(fed.domain_cluster.vars)

    def verifier(token: str) -> str:
        ks = trust.keyset()
        if ks is None:
            raise ApiError(503, "no signer key set in this cluster yet (is the domain agent running?)")
        return verify(token, ks, trust.revoked())["sub"]

    def consoles(cluster: str):
        raise ApiError(501, f"forwarding to {cluster}'s console needs service discovery wired here (nomadService)")

    api = ConsoleAPI(directory, consoles, verifier=verifier if os.environ.get("AUTH", "1") == "1" else None)
    console = Console(directory, view, api, refresh_interval=float(os.environ.get("REFRESH_INTERVAL", "5")))
    srv = console.serve(os.environ.get("CONSOLE_HOST", "0.0.0.0"), int(os.environ.get("CONSOLE_PORT", "8443")))
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    stop.wait()
    console.stop(srv)


if __name__ == "__main__":
    main()
