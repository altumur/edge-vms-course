"""The one-box console, standard library — the platform's SpecConsole run
from the VMS spec, plus the two routes only a VMS has (the bytes):

    GET  /timeline/<id>?from&to   segments from the archive resource's manifest, fenced ones marked
    GET  /segment/<path>          the bytes of one promoted segment from this box's archive, Range honoured

Everything else — the page, /spec, /cameras, /where, /marks, /metrics, the
POST/PUT/DELETE of a camera — is `vmsplatform.console.SpecConsole` reading
`vms.subsystem.yaml`; nothing here knows what a camera's fields are. Its own
process (`python3 -m vms console`), with its own token: the operator's rows —
cameras, next_id, retention — and never placement.
"""
from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from vmsplatform.console import PAGE, SpecConsole, send_file   # noqa: F401  (PAGE, send_file re-exported for М11)

from .archive import ArchiveResource, Manifest
from .controller import VmsController


def vms_routes(archive: ArchiveResource | None):
    """What the VMS adds to the generic console: the media. Returns None when
    a route is not ours, so the console answers 404."""
    def extra(handler, method, path, q):
        if method != "GET" or archive is None:
            return None
        if path.startswith("/segment/"):
            rel = path[len("/segment/"):]; p = os.path.join(archive.root, rel)
            if ".." in rel or not os.path.isfile(p):
                return 404, {"detail": "no such segment", "error": "no such segment"}
            send_file(handler, p, "video/mp4")
            return ()                                                     # served in full by send_file
        if path.startswith("/timeline/"):
            cid = int(path.rsplit("/", 1)[1])
            return 200, Manifest(archive.root, cid).timeline(float(q.get("from", 0)), float(q.get("to", 1e12)))
        return None
    return extra


def make_console(ctl: VmsController, archive: ArchiveResource | None, wall=None) -> SpecConsole:
    return SpecConsole(ctl, marks_root=archive.root if archive else None, wall=wall, extra=vms_routes(archive), media=archive is not None)


def serve(ctl: VmsController, archive: ArchiveResource | None, host: str = "127.0.0.1", port: int = 8080, wall=None) -> ThreadingHTTPServer:
    return make_console(ctl, archive, wall).serve(host, port)
