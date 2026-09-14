"""The cluster console, standard library — its own job (`system`: one on every server, nothing in front),
its own token (the operator's rows, never placement). The platform's
SpecConsole run from the VMS spec, exactly as М10 runs it; what a CLUSTER
adds is where the bytes and the events are — merged, not held:

    GET /timeline/<id>               merged across the resources that hold the camera; unreachable ones named
    GET /segment/<path>?server=<s>   the bytes of one segment, fetched from THAT server's resource job (Range passed through)
    GET /events?from&to&cam&…        merged across the live resources' own indexes (`MergedIndex`); no index here

The rest — the page, /spec, /cameras, /where (one scan of the assignments),
/resources, /unplaceable, /metrics, /marks, POST/PUT/DELETE — is
`psimplatform.console.SpecConsole` reading `vms.subsystem.yaml`. A console for
the det subsystem is the same class with a different YAML and no extra.

The console holds no event index. Each resource job indexes its own tree
(its buckets and the copies it holds of its peers') and answers
`GET /events` for it; the console asks every live resource and merges by
time — the way `/timeline` merges manifests — naming the ones that did not
answer, dropping a peer's copy when the owner itself answered, and fencing
each event by its unit's current epoch, which only the cluster's rows know.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from psimplatform.console import SpecConsole, heartbeats   # noqa: F401
from psimplatform.epoch import current_epoch
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


class MergedIndex:
    """What stands behind the console's `/events` on a cluster: nothing of its own.
    `query` asks every LIVE resource's `GET /events` (each answers from the index
    over its own tree — own buckets and mirror copies), merges by time, dedupes
    a dead server's copies when two peers hold them, drops a copy when the owner
    is live (it answered for itself), fences by `current_epochs`, and names in
    `state` the servers nobody answered for: `live; srv-a unreachable` for a
    resource that is silent and unmirrored (or live but not answering),
    `live; srv-a from mirror` when a peer's copy stood in. The same shape
    `EventIndex.query` returns, so `SpecConsole` cannot tell the difference."""

    def __init__(self, objects, fetch=None, wall=time.time, lost_after: float = 45.0, timeout: float = 3.0):
        self.objects, self.wall, self.lost_after, self.timeout = objects, wall, lost_after, timeout
        self.fetch = fetch or self._http
        self.state = "live"

    def _http(self, url: str, params: dict) -> dict:
        with urllib.request.urlopen(f"{url}/events?{urllib.parse.urlencode(params)}", timeout=self.timeout) as r:
            return json.loads(r.read())

    def query(self, t0: float, t1: float, cam=None, kind=None, subsystem=None, unit=None, current_epochs=None, limit: int = 1000) -> dict:
        now = self.wall(); seen = resources_seen(self.objects)
        live = {s for s, hb in seen.items() if now - float(hb["ts"]) <= self.lost_after}
        params = {k: v for k, v in (("from", t0), ("to", t1), ("cam", cam), ("kind", kind), ("subsystem", subsystem),
                                    ("unit", unit), ("limit", limit)) if v is not None}
        events, unreachable, from_mirror, have = [], [], set(), set()
        for server in sorted(live):
            try:
                rep = self.fetch(seen[server]["url"], params)
            except Exception:                                   # noqa: BLE001 — live by heartbeat, not answering
                unreachable.append(server); continue
            for e in rep["events"]:
                if e["server"] != server:                         # a copy this resource holds for a peer
                    if e["server"] in live:
                        continue                                  # the owner answers for itself
                    key = (e["server"], e["bucket"], e["t"], e["kind"], e["unit"])
                    if key in have:
                        continue                                  # two peers hold the same copy
                    have.add(key); from_mirror.add(e["server"])
                events.append(e)
        for server in sorted(seen):
            if server not in live and server not in from_mirror:
                unreachable.append(server)                        # silent, and nobody holds its copies
        events.sort(key=lambda e: e["t"]); events = events[:limit]
        cur = current_epochs or {}
        for e in events:
            c = cur.get((e["subsystem"], e["unit"]))
            e["fenced"] = c is not None and e["epoch"] < c
        unreachable = sorted(set(unreachable))
        self.state = "live" + (f"; {', '.join(unreachable)} unreachable" if unreachable else "") \
                            + (f"; {', '.join(sorted(from_mirror))} from mirror" if from_mirror else "")
        return {"events": events, "state": self.state}


def make_console(ctl: ClusterController, reader=None, worst_failover: float = 0.0, index=None, archive_root: str | None = None) -> SpecConsole:
    return SpecConsole(ctl, marks_root=archive_root, index=index or MergedIndex(ctl.objects, wall=ctl.wall), worst_failover=worst_failover,
                       extra=cluster_routes(ctl, reader), media=True)


def metrics_text(ctl: ClusterController, worst_failover: float) -> str:
    return SpecConsole(ctl, worst_failover=worst_failover).metrics_text()


def serve(ctl, host="127.0.0.1", port=8080, reader=None, worst_failover=0.0, index=None, archive_root=None) -> ThreadingHTTPServer:
    return make_console(ctl, reader, worst_failover, index, archive_root).serve(host, port)
