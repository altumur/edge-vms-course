"""The resource — a platform job, one per server, pinned there for as long
as the server exists. It knows the shape of what every subsystem leaves on
a server's disks and nothing about what it means:

    <root>/<subsystem>/<unit>/e<epoch>/...          each subsystem's tree: its buckets, and whatever else it
                                                    keeps beside them (the VMS: media and a manifest — its own)
    <root>/.mirror/<server>/<subsystem>/<unit>/...  copies of another server's closed buckets (the knob)

    platform/resources/<server>/heartbeat   {server, ts, url, usage, units: {sub: [unit]}, mirrors: {server: n}}
    platform/mirror                         the knob: {enabled, copies}
    <sub>/retention, <sub>/retention/<unit> {days}: each subsystem's policy for its buckets, written by ITS controller

    GET  <url>/buckets/<sub>/<unit>    closed buckets, from the files
    GET  <url>/events/<path>           one bucket (also .mirror/<server>/<path>)
    GET  <url>/mirrored/<server>       which of <server>'s buckets this server holds copies of
    GET  <url>/events?from&to&cam&kind&subsystem&unit   this resource's EventDatabase: its buckets and its copies
    PUT  <url>/mirror/<server>/<path>  another resource leaves a copy of one of ITS closed buckets here

The policy pass runs on a timer: retain each subsystem's buckets by its
policy; mirror closed buckets to the next live resource(s) after this one
in sorted order — nobody assigns peers, the rule is the assignment; and any
subsystem-specific pass a subsystem registered (the VMS registers its
media repair and retention). `restore` is the reverse of mirror, run by
the owner at start: a server back with an empty disk pulls its buckets
home. No controller is involved in any of it.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # resource.py — the resource as a platform job: heartbeat, buckets over HTTP, retention by each
# subsystem's
# row, mirror to a peer, restore
#
# **Role in the module.** Lesson 3. One resource per server, pinned there for as long as the server exists.
# It knows the shape of what every subsystem leaves on the server's disks —
# `<root>/<subsystem>/<unit>/e<epoch>/...` — and nothing about what it means; the VMS keeps media and a
# manifest beside its buckets and the resource neither reads nor names them. It writes its own heartbeat
# object (`platform/resources/<server>/heartbeat`), serves buckets over HTTP, and runs a policy pass on a
# timer: each subsystem's registered hook first (the VMS registers repair, close and media retention via
# `Resource.register`), then bucket retention by each subsystem's own `<sub>/retention[/<unit>]` row, then
# the mirror. Mirroring is a knob (`platform/mirror`), and peers are chosen by a rule — the next `copies`
# live resources after mine in sorted order — so nobody assigns them. `restore` is the reverse, run by the
# owner at start. No controller is involved in any of it. `eventdatabase.EventDatabase` is the database the
# job runs over this tree (`Resource.database`), served as `GET /events` and told by `retain` what it removed;
# `console.py` reads `resources_seen`.
#
# ## Module-level names
# - `MIRROR_DIR = ".mirror"` — under a resource root, `.mirror/<server>/<sub>/<unit>/e<epoch>/…` holds
#   copies of another server's closed buckets. Hidden so `subsystems_under` never counts it as this server's
#   data.
# - `MIRROR_KEY = "platform/mirror"` — the Variable `{enabled, copies}`.
# - `RESOURCES = "platform/resources"` — the object-store prefix for resource heartbeats.
#
# ### `__init__(self, root, server, url, vars_, objects, bucket_seconds=600, wall=time.time, peers=None,
# lost_after=45.0)` `root` is the tree (created), `server` the name that goes into heartbeats and peer
# selection, `url` how others reach this resource's HTTP. `hooks` starts empty. `lost_after` is how old a
# peer's heartbeat may be to count as live.
#
# ## Notes
# - The heartbeat's `units` and `mirrors` are derived from the tree on every call — the resource keeps no
#   state a restart could lose.
# - `retain` and `mirror` both walk the tree each pass; on one box that is cheap, and it keeps the job
#   stateless.
# - The console's `/resources` route is `resources_seen` with a `live | silent` label by `lost_after`;
#   `/metrics` counts `<sub>_resources_live` the same way.
# ================================================================================================
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .events import Bucket, buckets_under, parse_bucket, subsystems_under

MIRROR_DIR = ".mirror"
MIRROR_KEY = "platform/mirror"
RESOURCES = "platform/resources"


# Parses the JSON line `Bucket.line()` produced (types coerced back). Used by `PeerClient`.
def bucket_from_line(line: str) -> Bucket:
    d = json.loads(line)
    return Bucket(d["subsystem"], str(d["unit"]), int(d["epoch"]), float(d["start"]), float(d["end"]), d["path"], int(d["events"]))


# Reads the knob: `enabled` is true only if the row exists and says `"true"`; `copies` defaults to 1.
def mirror_settings(vars_) -> dict:
    items, _ = vars_.get(MIRROR_KEY)
    return {"enabled": bool(items) and items.get("enabled") == "true", "copies": int((items or {}).get("copies", 1))}


# The unit's days if its subsystem set `<sub>/retention/<unit>`, else the subsystem's `<sub>/retention`,
# else a year. For the VMS the per-unit row is the derived row `vms/retention/<id>` written by
# `SpecController._derived` from `events_retention_days`; on delete it becomes `{days: 0}` so the buckets go
# on the next pass. Each subsystem's controller owns its row; the resource only reads.
def retention_days(vars_, subsystem: str, unit: str, default: float = 365.0) -> float:
    """The unit's days if its subsystem set them, else the subsystem's, else a year."""
    for path in (f"{subsystem}/retention/{unit}", f"{subsystem}/retention"):
        items, _ = vars_.get(path)
        if items and "days" in items:
            return float(items["days"])
    return default


# The rule that replaces a map: sort the other live servers, take those after mine then wrap around, and
# keep the first `copies`. `test_the_resource_is_a_platform_job…`: with `srv-a, srv-b, srv-c`, `srv-a`'s
# peer is `srv-b` and `srv-c`'s is `srv-a`.
def peers_of(server: str, live: list[str], copies: int) -> list[str]:
    """The rule that replaces a map: the next `copies` live resources after mine, in sorted order."""
    others = sorted(s for s in live if s != server)
    if not others:
        return []
    after = [s for s in others if s > server] + [s for s in others if s < server]
    return after[:copies]


# Every resource heartbeat under `platform/resources/`, keyed by `server`, whatever its age. Callers filter
# by `ts`.
def resources_seen(objects) -> dict[str, dict]:
    out = {}
    for key in objects.list(RESOURCES + "/"):
        if key.endswith("/heartbeat"):
            raw = objects.get(key)
            if raw:
                hb = json.loads(raw)
                out[hb["server"]] = hb
    return out


# The server names present under `<root>/.mirror/`.
def mirrored_servers(root: str) -> list[str]:
    try:
        return sorted(d for d in os.listdir(os.path.join(root, MIRROR_DIR)) if os.path.isdir(os.path.join(root, MIRROR_DIR, d)))
    except FileNotFoundError:
        return []


# Copies this resource holds of `server`'s buckets, parsed relative to `.mirror/<server>` so `path` is the
# original path on `server`. Line counts are taken from the copy.
def mirrored_buckets(root: str, server: str, bucket_seconds: int = 600) -> list[Bucket]:
    """Copies this resource holds of <server>'s buckets; `path` is the ORIGINAL path on <server>."""
    base = os.path.join(root, MIRROR_DIR, server)
    out = []
    for d, _, files in os.walk(base):
        for f in files:
            p = os.path.join(d, f)
            parsed = parse_bucket(p, base)
            if parsed:
                sub, unit, epoch, start = parsed
                with open(p) as fh:
                    n = sum(1 for l in fh if l.strip())
                out.append(Bucket(sub, unit, epoch, start, start + bucket_seconds, os.path.relpath(p, base), n))
    return sorted(out, key=lambda b: (b.start, b.epoch))


# How one resource talks to another: HTTP. Tests substitute an in-process client with the same three methods
# over directories.
class PeerClient:
    """How one resource talks to another: HTTP; tests substitute an in-process client."""
    def __init__(self, timeout: float = 5.0): self.timeout = timeout

    # `GET <url>/mirrored/<server>` — which of `server`'s buckets the peer already holds.
    def mirrored(self, url: str, server: str) -> list[Bucket]:
        with urllib.request.urlopen(f"{url}/mirrored/{server}", timeout=self.timeout) as r:
            return [bucket_from_line(l) for l in r.read().decode().splitlines() if l.strip()]

    # `PUT <url>/mirror/<server>/<path>` with the bucket's bytes; anything but 200/201/204 raises `IOError`.
    def put(self, url: str, server: str, path: str, data: bytes) -> None:
        req = urllib.request.Request(f"{url}/mirror/{server}/{path}", data=data, method="PUT")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.status not in (200, 201, 204):
                raise IOError(f"PUT mirror {path}: {r.status}")

    # `GET <url>/events/.mirror/<server>/<path>` — pull a copy back (restore).
    def get(self, url: str, server: str, path: str) -> bytes:
        with urllib.request.urlopen(f"{url}/events/{MIRROR_DIR}/{server}/{path}", timeout=self.timeout) as r:
            return r.read()


# One server's resource: its tree, its heartbeat, its policy pass.
class Resource:
    """One server's resource: its tree, its heartbeat, its policy pass."""

    def __init__(self, root: str, server: str, url: str, vars_, objects, bucket_seconds: int = 600,
                 wall=time.time, peers: PeerClient | None = None, lost_after: float = 45.0):
        self.root, self.server, self.url, self.vars, self.objects = root, server, url, vars_, objects
        self.bucket_seconds, self.wall, self.peers, self.lost_after = bucket_seconds, wall, peers or PeerClient(), lost_after
        self.hooks: dict[str, object] = {}         # subsystem -> object with .pass_(now) -> dict: its own policy on ITS part of the tree
        self.database = None                       # an eventdatabase.EventDatabase over this tree, if the job runs one: served as GET /events
        os.makedirs(root, exist_ok=True)

    # A subsystem installs an object with `pass_(now) -> dict` for its own part of the tree — the same "code
    # under a name" door `spec.register_constraint` opens.
    def register(self, subsystem: str, hook) -> None:
        self.hooks[subsystem] = hook

    # -- what is here -------------------------------------------------------------------
    # `subsystems_under(root)` — what is here, from the directories.
    def units(self) -> dict[str, list[str]]:
        return subsystems_under(self.root)

    # Every bucket of every unit whose `end <= now`. Only these are mirrored.
    def closed_buckets(self) -> list[Bucket]:
        out = []
        for sub, units in self.units().items():
            for unit in units:
                out += [b for b in buckets_under(self.root, sub, unit, self.bucket_seconds) if b.end <= self.wall()]
        return out

    # Total bytes under `root`, for the heartbeat.
    def usage(self) -> int:
        total = 0
        for d, _, files in os.walk(self.root):
            for f in files:
                total += os.path.getsize(os.path.join(d, f))
        return total

    # Writes `{server, ts, url, usage, units, mirrors: {server: n copies}}` to
    # `platform/resources/<server>/heartbeat` and returns it. `units` is how the index discovers subsystems;
    # `mirrors` is how `restore` and the index find who holds copies.
    def heartbeat(self) -> dict:
        hb = {"server": self.server, "ts": self.wall(), "url": self.url, "usage": self.usage(), "units": self.units(),
              "mirrors": {s: len(mirrored_buckets(self.root, s, self.bucket_seconds)) for s in mirrored_servers(self.root)}}
        self.objects.put(f"{RESOURCES}/{self.server}/heartbeat", json.dumps(hb).encode())
        return hb

    # `resources_seen` filtered to heartbeats younger than `lost_after`.
    def live_resources(self) -> dict[str, dict]:
        now = self.wall()
        return {s: hb for s, hb in resources_seen(self.objects).items() if now - float(hb["ts"]) <= self.lost_after}

    # -- the policy pass ------------------------------------------------------------------
    # For each subsystem and unit, delete bucket files whose `end` is older than `retention_days` — files
    # only; a subsystem that indexes its buckets (the VMS's manifest) drops the lines in its own hook. The
    # resource's own database forgets each removed path. Returns the count. The test sets `other/retention {days: 1}`, advances three days and sees exactly the
    # `other` bucket go.
    def retain(self) -> int:
        """Each subsystem's buckets by its own days. Files only: a subsystem that
        indexes its buckets (the VMS's manifest) drops the lines in its own pass."""
        removed = []
        for sub, units in self.units().items():
            for unit in units:
                days = retention_days(self.vars, sub, unit)
                for b in buckets_under(self.root, sub, unit, self.bucket_seconds):
                    if b.end < self.wall() - days * 86400:
                        os.remove(os.path.join(self.root, b.path)); removed.append(b.path)
        if removed and self.database is not None:
            self.database.forget(self.server, removed)                  # the rows go with the file
        return len(removed)

    # The knob. If disabled, `{enabled: False, mirrored: 0, peers: []}`. Otherwise, for each peer from
    # `peers_of`, ask what it already holds and `put` every closed bucket it lacks — any subsystem's,
    # exactly once each, by the server that owns it. Returns `{enabled, mirrored, peers}`. The test shows
    # two buckets mirrored the first pass and zero the second.
    def mirror(self) -> dict:
        """The knob. Every CLOSED bucket on this server — any subsystem — is
        copied to the next live resource(s) after it, exactly once each (the
        peer says what it already holds), by the server that owns it."""
        knob = mirror_settings(self.vars)
        if not knob["enabled"]:
            return {"enabled": False, "mirrored": 0, "peers": []}
        live = self.live_resources()
        peers = peers_of(self.server, list(live), knob["copies"])
        n = 0
        for peer in peers:
            have = {b.path for b in self.peers.mirrored(live[peer]["url"], self.server)}
            for b in self.closed_buckets():
                if b.path in have:
                    continue
                with open(os.path.join(self.root, b.path), "rb") as f:
                    self.peers.put(live[peer]["url"], self.server, b.path, f.read())
                n += 1
        return {"enabled": True, "mirrored": n, "peers": peers}

    # The reverse, run by the owner: for every live peer whose heartbeat lists me under `mirrors`, pull each
    # of my buckets it holds that I do not have (tmp + rename), then, if anything came back, run every
    # registered hook once so the subsystem re-indexes. Returns `{pulled, <sub>.<key>: …}`. In the test,
    # `srv-a` with a wiped disk pulls 2 buckets; the open bucket that was never mirrored is the RPO.
    def restore(self) -> dict:
        """The reverse, run by the owner: pull my buckets from whoever holds
        copies, then let each subsystem's hook re-index what came back."""
        pulled = 0
        for peer, hb in self.live_resources().items():
            if peer == self.server or self.server not in hb.get("mirrors", {}):
                continue
            for path in sorted(b.path for b in self.peers.mirrored(hb["url"], self.server)):
                dest = os.path.join(self.root, path)
                if os.path.exists(dest):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".tmp", "wb") as f:
                    f.write(self.peers.get(hb["url"], self.server, path))
                os.replace(dest + ".tmp", dest); pulled += 1
        hooks = {sub: h.pass_(self.wall()) for sub, h in self.hooks.items()} if pulled else {}
        return {"pulled": pulled, **{f"{s}.{k}": v for s, r in hooks.items() for k, v in r.items()}}

    # The timer's body, in order: each subsystem's hook (it may index or drop lines), then `retain`, then
    # `mirror`; results flattened into one dict (`<sub>.<key>`, `removed`, `enabled`, `mirrored`, `peers`).
    def pass_(self) -> dict:
        out = {}
        for sub, h in self.hooks.items():                    # a subsystem's own pass first: it may index or drop lines
            out.update({f"{sub}.{k}": v for k, v in h.pass_(self.wall()).items()})
        out["removed"] = self.retain()
        out.update(self.mirror())
        return out


# The resource over HTTP, in a daemon thread. `extra(path, headers) -> (status, bytes[, headers]) | None`
# lets a subsystem add its own reads (the VMS: manifests and footage).
#
# #### `class H(BaseHTTPRequestHandler)` (nested)
# - `log_message` — silenced.
# - `_raw(status, body, headers=())` — send a status, `Content-Length`, optional headers and the bytes.
# - `do_GET`:
#   - `GET /buckets/<sub>/<unit>` — `buckets_under` for that unit, one `Bucket.line()` per line, 200.
#   - `GET /mirrored/<server>` — `mirrored_buckets` for that server, same format.
#   - `GET /events?from&to&cam&kind&subsystem&unit&limit` — `resource.database.query(...)` as JSON
#     (`{events, state}`, unfenced: the console fences); 503 if the job runs no database.
#         - `GET /events/<path>` — the raw bytes of one bucket; `path` may begin with `.mirror/<server>/`.
#       404 if it contains `..`, does not end in `.events.jsonl`, or is not a file.
#   - anything else — `extra(path, headers)` if given and it answers; otherwise 404.
# - `do_PUT`:
#         - `PUT /mirror/<server>/<path>` — another resource leaves a copy of one of its closed buckets. 400
#       if `..`, empty server, or not `.events.jsonl`; writes to `.mirror/<server>/<path>` via tmp + rename
#       (a copy appears whole or not at all); 204. Any other PUT is 404.
def serve(resource: Resource, host: str = "0.0.0.0", port: int = 8090, extra=None) -> ThreadingHTTPServer:
    """The resource over HTTP. `extra(path) -> (status, bytes) | None` lets a
    subsystem add its own reads (the VMS: manifests and footage)."""
    root = resource.root

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _raw(self, status, body, headers=()):
            self.send_response(status); self.send_header("Content-Length", str(len(body)))
            for k, v in headers: self.send_header(k, v)
            self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/buckets/"):
                _, _, sub, unit = self.path.split("/", 3)
                return self._raw(200, "".join(b.line() + "\n" for b in buckets_under(root, sub, unit, resource.bucket_seconds)).encode())
            if self.path.startswith("/mirrored/"):
                return self._raw(200, "".join(b.line() + "\n" for b in mirrored_buckets(root, self.path[len("/mirrored/"):], resource.bucket_seconds)).encode())
            if self.path == "/events" or self.path.startswith("/events?"):
                if resource.database is None:
                    return self._raw(503, b'{"error": "this resource runs no event database"}', [("Content-Type", "application/json")])
                q = {k: v[0] for k, v in urllib.parse.parse_qs(self.path.partition("?")[2]).items()}
                rep = resource.database.query(float(q.get("from", 0)), float(q.get("to", 1e12)),
                                           int(q["cam"]) if q.get("cam") else None, q.get("kind"), q.get("subsystem"), q.get("unit"),
                                           limit=int(q.get("limit", 1000)))
                return self._raw(200, json.dumps(rep).encode(), [("Content-Type", "application/json")])
            if self.path.startswith("/events/"):
                rel = self.path[len("/events/"):]; p = os.path.join(root, rel)
                if ".." in rel or not rel.endswith(".events.jsonl") or not os.path.isfile(p):
                    return self._raw(404, b"")
                with open(p, "rb") as f: return self._raw(200, f.read())
            if extra is not None:
                r = extra(self.path, self.headers)
                if r is not None:
                    return self._raw(*r)
            self._raw(404, b"")

        def do_PUT(self):
            if not self.path.startswith("/mirror/"):
                return self._raw(404, b"")
            rel = self.path[len("/mirror/"):]
            server, _, path = rel.partition("/")
            if ".." in rel or not server or not path.endswith(".events.jsonl"):
                return self._raw(400, b"")
            dest = os.path.join(root, MIRROR_DIR, server, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            n = int(self.headers.get("Content-Length", 0))
            with open(dest + ".tmp", "wb") as f:
                f.write(self.rfile.read(n))
            os.replace(dest + ".tmp", dest)                     # a copy appears whole or not at all
            self._raw(204, b"")

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
