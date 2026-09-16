"""python3 -m domain.signer_service — the domain signer as a process.

Holds the keys (from domain/signer in the domain cluster's raft), publishes
the key set and the revocation list for the agents, publishes the identity
set object-first on a floor, and answers logins with tokens. The CA half
(issue, renew, rotate) is driven by the registrar and by renewal requests
over mTLS, which need the bench.

    POST /login          {"user","password"}         -> {"token"}
    POST /revoke         {"token"}                    -> revokes that token's jti
    GET  /keys           the key set (what agents copy)
"""
from __future__ import annotations

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cluster.objectstore import open_store
from cluster.variables import NomadVariables

from .agent import DomainPublisher
from .identity import AuthError, IdentityStore
from .signer import Signer
from .tokens import RevocationList, verify
import cluster as _cluster  # noqa: F401  — registers the `nomad://` scheme
from w2cplatform.variables import open_vars


def main() -> None:
    domain = os.environ.get("DOMAIN_ID", "domain")
    vars_ = open_vars(os.environ.get("CONFIG_URL") or "nomad://" + os.environ.get("NOMAD_ADDR", "127.0.0.1:4646").replace("http://", ""))
    objects = open_store(os.environ.get("OBJECT_STORE_URL", "file:///data/domain"))
    signer = Signer(domain, vars_)
    ids = IdentityStore(signer, vars_, objects, publish_floor=float(os.environ.get("IDENTITY_PUBLISH_FLOOR", "60")))
    pub = DomainPublisher(vars_)
    revoked = RevocationList.from_items(vars_.get("domain/revoked")[0])
    pub.publish_keys(signer.tokens.keyset())

    class H(BaseHTTPRequestHandler):
        def _send(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/keys":
                return self._send(200, signer.tokens.keyset().to_items())
            self._send(404, {"detail": "no such route"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            try:
                if self.path == "/login":
                    return self._send(200, {"token": ids.login(body["user"], body["password"])})
                if self.path == "/revoke":
                    revoked.revoke(verify(body["token"], signer.tokens.keyset()))
                    pub.publish_revoked(revoked)
                    return self._send(200, {"revoked": True})
            except AuthError:
                return self._send(401, {"detail": "bad credentials"})
            self._send(404, {"detail": "no such route"})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((os.environ.get("SIGNER_HOST", "0.0.0.0"), int(os.environ.get("SIGNER_PORT", "8445"))), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    while not stop.is_set():
        try:
            ids.publish()                                     # object first, then the pointer, on a floor
            revoked.prune(__import__("time").time())
        except Exception:                                     # noqa: BLE001
            pass
        stop.wait(5)
    srv.shutdown()


if __name__ == "__main__":
    main()
