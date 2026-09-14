"""The cluster console, standard library — its own job (`system`: one on every server, nothing in front),
its own token (the operator's rows, never placement). The platform's
SpecConsole run from the VMS spec, exactly as М10 runs it; what a CLUSTER
adds is where the bytes and the events are — merged, not held:

    GET /timeline/<id>               merged across the resources that hold the camera; unreachable ones named
    GET /segment/<path>?server=<s>   the bytes of one segment, fetched from THAT server's resource job (Range passed through)
    GET /events?from&to&cam&…        merged across the live resources' event databases (the platform's `MergedIndex`); none here

The rest — the page, /spec, /cameras, /where (one scan of the assignments),
/resources, /unplaceable, /metrics, /marks, POST/PUT/DELETE — is
`psimplatform.console.SpecConsole` reading `vms.subsystem.yaml`. A console for
the det subsystem is the same class with a different YAML and no extra.

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

from psimplatform.console import SpecConsole, heartbeats   # noqa: F401
from psimplatform.epoch import current_epoch
from psimplatform.eventdatabase import MergedIndex          # noqa: F401  (re-exported: the console's view of the event databases)
from psimplatform.resource import resources_seen

from .controller import ClusterController
from .timeline import ManifestReader, merged_timeline


def cluster_routes(ctl: ClusterController, reader=None):
    """The cluster's media routes: the timeline is merged, the segment is proxied."""
    reader = reader or ManifestReader()

    def extra(handler, method, path, q):
        if method != "GET":
            return None
        if path.startswith("/timeline/"):
            cid = int(path.rsplit("/", 1)[1])
            cur = current_epoch(ctl.vars, ctl.sub.epoch_key(str(cid))) or None
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


def make_console(ctl: ClusterController, reader=None, worst_failover: float = 0.0, index=None, archive_root: str | None = None) -> SpecConsole:
    return SpecConsole(ctl, marks_root=archive_root, index=index or MergedIndex(ctl.objects, wall=ctl.wall), worst_failover=worst_failover,
                       extra=cluster_routes(ctl, reader), media=True)


def metrics_text(ctl: ClusterController, worst_failover: float) -> str:
    return SpecConsole(ctl, worst_failover=worst_failover).metrics_text()


def serve(ctl, host="127.0.0.1", port=8080, reader=None, worst_failover=0.0, index=None, archive_root=None) -> ThreadingHTTPServer:
    return make_console(ctl, reader, worst_failover, index, archive_root).serve(host, port)
