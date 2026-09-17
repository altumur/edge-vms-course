"""The cluster console, standard library — its own job (`system`: one on every server, nothing in front),
its own token (the operator's rows, never placement). The platform's
SpecConsole run from the VMS spec, exactly as М10 runs it; what a CLUSTER
adds is where the bytes and the events are — merged, not held:

    GET /timeline/<id>               merged across the resources that hold the camera's RECORDING (rec/<cam>); unreachable ones named
    GET /segment/<path>?server=<s>   the bytes of one segment, fetched from THAT server's resource job (Range passed through)
    GET /events?from&to&cam&…        merged across the live resources' event databases (the platform's `MergedIndex`); none here
    /rec/spec, /rec/recordings, …    the recorder mounted under its name (`Mount`): the page's Record toggle POSTs here

The rest — the page, /spec, /cameras, /where (one scan of the assignments),
/resources, /servers, /policy, /unplaceable, /metrics, /marks, POST/PUT/DELETE — is
`w2cplatform.console.SpecConsole` reading `vms.subsystem.yaml`. The recorder's
console at `/rec/…` is the same class over `rec.subsystem.yaml` and no extra.

The console holds no event database. Each resource job keeps one over its
own tree (its buckets and the copies it holds of its peers') and answers
`GET /events` from it; the console asks every live resource and merges by
time — the way `/timeline` merges manifests — naming the ones that did not
answer, dropping a peer's copy when the owner itself answered, and fencing
each event by its unit's current epoch, which only the cluster's rows know.
Exactly what М10's console does with its one resource process: one class.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from w2cplatform.console import Mount, SpecConsole, heartbeats   # noqa: F401
from w2cplatform.epoch import current_epoch
from w2cplatform.eventdatabase import MergedIndex          # noqa: F401  (re-exported: the console's view of the event databases)
from w2cplatform.resource import resources_seen
from w2cplatform.spec import SpecController

from .controller import ClusterController
from .timeline import ManifestReader, merged_timeline


def cluster_routes(ctl: ClusterController, reader=None):
    """The cluster's media routes: the timeline is merged, the segment is proxied."""
    reader = reader or ManifestReader()

    def extra(handler, method, path, q):
        if method != "GET":
            return None
        if path.startswith("/timeline/"):
            cid = path.rsplit("/", 1)[1]                                   # the recording's id, verbatim — a recording is named
            cur = current_epoch(ctl.vars, f"rec/epoch/{cid}") or None      # the RECORDING's epoch: footage is the recorder's, fenced by its writer
            return 200, merged_timeline(resources_seen(ctl.objects), reader, cid,
                                        float(q.get("from", 0)), float(q.get("to", 1e12)), cur, ctl.wall())
        if path.startswith("/segment/"):
            rel = path[len("/segment/"):]; res = resources_seen(ctl.objects).get(q.get("server", ""))
            if ".." in rel or res is None:
                return 404, {"error": "no such resource", "detail": "no such resource"}
            req = urllib.request.Request(f"{res['url']}/segment/{rel}",
                                         headers={k: v for k, v in (("Range", handler.headers.get("Range")),) if v})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read(); status = r.status; crange = r.headers.get("Content-Range")
            except urllib.error.HTTPError as e:
                return e.code, {"error": f"the resource on {q['server']} said {e.code}"}
            except OSError:
                return 503, {"error": f"the resource on {q['server']} is not answering — unavailable, not lost"}
            headers = [("Content-Type", "video/mp4"), ("Accept-Ranges", "bytes")] + ([("Content-Range", crange)] if crange else [])
            return status, data, headers
        return None
    return extra


def make_console(ctl: ClusterController, reader=None, worst_failover: float = 0.0, index=None, archive_root: str | None = None,
                 rec_ctl: SpecController | None = None) -> Mount:
    """The VMS at `/` and, when the console fronts it, the recorder at `/rec/…` (the page's Record toggle:
    POST /rec/recordings). Both answer /events from the same merge over the resources' databases."""
    index = index or MergedIndex(ctl.objects, wall=ctl.wall)
    root = SpecConsole(ctl, marks_root=archive_root, index=index, worst_failover=worst_failover, extra=cluster_routes(ctl, reader), media=True)
    m = Mount(root)
    if rec_ctl is not None:
        m.mount("rec", SpecConsole(rec_ctl, wall=ctl.wall, index=index))
    return m


def metrics_text(ctl: ClusterController, worst_failover: float) -> str:
    return SpecConsole(ctl, worst_failover=worst_failover).metrics_text()


def serve(ctl, host="127.0.0.1", port=8080, reader=None, worst_failover=0.0, index=None, archive_root=None, rec_ctl=None) -> ThreadingHTTPServer:
    return make_console(ctl, reader, worst_failover, index, archive_root, rec_ctl).serve(host, port)
