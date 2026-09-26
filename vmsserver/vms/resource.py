"""The resource process — the platform's resource job (w2cplatform.resource)
with the VMS registered on it. One per server, pinned there for as long as
the server exists; on a box it is `python3 -m vms resource`
(`deploy/resource.container`), in М11 the `resource` system job. It has
no controller: it has a policy pass on a timer, a heartbeat, its HTTP, and
the event database over its own tree. What the VMS adds is its part:

    ArchivePolicy   registered as the "rec" hook: the recorder's — repair the manifests, retain media by rec/recordings/<unit>,
                    and free bytes when the disk is over its watermark (vms/space.py)
    vms_routes      GET /manifest/<unit>  the manifest's lines;  GET /segment/<path>  the bytes, Range honoured;
                    GET /space  what is here, how deep it goes, and what of it another server writes now
    vms_writes      PUT /segment/<path>   a segment arriving from the resource that is giving it up

    platform/resources/<server>/heartbeat   {server, ts, url, usage, units, mirrors} — how the console finds it
    GET <url>/events?from&to&cam&kind&subsystem&unit   the platform's: this resource's EventDatabase
    GET <url>/buckets/<sub>/<unit>, /events/<path>, /mirrored/<server>; PUT /mirror/<server>/<path>

Nothing about heartbeats, buckets, mirrors or the database is the VMS's, and
the names say so: platform/resources/<server>/heartbeat, platform/mirror.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # resource.py — the resource process: the platform's Resource with the VMS's policy, routes and the event
# database
#
# **Role in the module.** Lesson 10 (events) and Lesson 11 (the box). The archive was a resource from Lesson
# 3 — pinned, registered on, never placed — and this is the process that stands for it: the platform's
# `Resource` over `/data/archive` with `ArchivePolicy` registered as the `rec` hook (the recorder's: manifest
# repair, media retention by the recording row), a heartbeat under `platform/resources/<server>`, the platform's HTTP (`w2cplatform.resource.
# serve`) with the VMS's two reads added, and an `EventDatabase` over the tree, rebuilt on start and tailed,
# served as `GET /events`. The console holds no database: it asks this process (`MergedIndex`). М11 runs the
# same function as the `resource` job on every server (`cluster/resource.py` re-exports it), which is the
# point: the box and the cluster have one architecture.
#
# ## Module-level names
# None.
#
# ### `vms_routes(archive) -> extra(path, headers)`
# The VMS's reads on the resource, plugged into the platform's server: `GET /manifest/<unit>` returns the
# manifest's raw lines; `GET /segment/<path>` the bytes of one promoted segment with `Range` honoured (206 +
# `Content-Range`), 404 for `..` or a missing file. М11's console proxies `/segment` to this.
#
# ### `vms_resource(archive, server, url, vars_, objects, wall=None, peers=None, database=":memory:") -> Resource`
# Builds the platform's `Resource` on the archive's root with the archive's `bucket_seconds` and clock,
# registers `ArchivePolicy(archive, vars_)` as the `rec` hook (footage is the recorder's subsystem; the
# worker's tree under `vms/` holds events only, retained by the platform's bucket policy), and attaches `resource.database =
# EventDatabase(root, server, database, wall, bucket_seconds)` — created, not started: the process calls
# `start()` after `restore()`, a test calls `rebuild()`/`tail()` by hand. `retain()` tells the database what
# it removed; `serve()` answers `/events` from it.
#
# ## Notes
# - The old `vms-archive-retain.timer` ran the same policy as a oneshot every ten minutes; the process runs
#   it every 600 s from its loop and adds what a oneshot could not hold: a heartbeat, a port, a database.
# ================================================================================================
from __future__ import annotations

import json
import os

from w2cplatform.eventdatabase import EventDatabase
from w2cplatform.resource import Resource

from .archive import SUB, ArchivePolicy, ArchiveResource, Manifest, Segment


def vms_routes(archive: ArchiveResource, objects=None, server: str = ""):
    """The VMS's reads on the resource, plugged into the platform's server."""
    root = archive.root

    def extra(path: str, headers):
        if path == "/space" or path.startswith("/space?"):
            # What the archive holds and what of it is not ours — the state the watermark acts on, readable
            # whether or not it is acting. The operator asks "where is camera 7's footage" and "how many
            # days do I actually have"; `retention_days` only ever promised, and a unit written here for a
            # neighbour is invisible until somebody says so.
            from .space import depth_days, foreign, unit_bytes
            now = archive.wall()
            away = foreign(archive, objects, server, now) if objects is not None and server else {}
            units = {u: {"bytes": unit_bytes(archive, u), "days": round(depth_days(archive, u, now), 2),
                         **({"written_on": away[u]} if u in away else {})}
                     for u in archive.units()}
            # `accounted` is what the manifests name. The resource's heartbeat says `usage` — every FILE
            # under the root — and the gap between the two is what nobody indexes: spool leftovers, a
            # half-written segment, somebody's tarball on the same disk. Worth seeing side by side.
            return 200, json.dumps({"server": server, "units": units, "foreign": away,
                                    "accounted": sum(u["bytes"] for u in units.values())}).encode(), (("Content-Type", "application/json"),)
        if path.startswith("/manifest/"):
            unit = path.rsplit("/", 1)[1]                 # a UNIT, verbatim: "7" today, "7-backup" the day the spec says so
            return 200, "".join(l for l in Manifest(root, unit)._lines()).encode()
        if path.startswith("/segment/"):
            rel = path[len("/segment/"):]
            p = os.path.join(root, rel)
            if ".." in rel or not os.path.isfile(p):
                return 404, b""
            size = os.path.getsize(p); start, end = 0, size - 1
            rng = headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, b = rng[6:].split("-"); start = int(a or 0); end = int(b) if b else end
            with open(p, "rb") as f:
                f.seek(start); data = f.read(end - start + 1)
            return (206 if rng else 200), data, ((("Content-Range", f"bytes {start}-{end}/{size}"),) if rng else ())
        return None
    return extra


def vms_writes(archive: ArchiveResource):
    """The VMS's one write on the resource: a segment another resource is giving up.

    It arrives as bytes plus its manifest line in `X-Segment`, and lands at the SAME
    relative path — `rec/<unit>/e<epoch>/<stamp>.mp4` says nothing about a server, which
    is why footage can change hands at all. The epoch travels with it: it says who
    WROTE the segment, never where it lies, and a timeline marks it fenced by comparing
    with the current one exactly as before.

    Idempotent on purpose: a path already in our manifest is accepted again and the line
    is not doubled, so the sender may retry a batch it is unsure of. Written tmp + rename,
    the file first and the line after — the order `promote` uses, for the same reason."""
    root = archive.root

    def extra_put(path: str, headers, rfile):
        if not path.startswith("/segment/"):
            return None
        rel = path[len("/segment/"):]
        if ".." in rel or not rel.startswith(SUB + "/") or not rel.endswith(".mp4"):
            return 400, b""
        line = headers.get("X-Segment")
        if not line:
            return 400, b'{"error": "a segment arrives with its manifest line"}'
        try:
            seg = Segment.from_line(line)
        except (ValueError, KeyError):
            return 400, b'{"error": "a segment arrives with its manifest line"}'
        if seg.path != rel:
            return 400, b'{"error": "the line does not describe this path"}'
        dest = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        n = int(headers.get("Content-Length", 0))
        with open(dest + ".tmp", "wb") as f:
            f.write(rfile.read(n))
        os.replace(dest + ".tmp", dest)                     # whole or not at all
        man = Manifest(root, seg.unit)
        if all(s.path != rel for s in man.read()):          # a retried segment does not get a second line
            man.append(seg)
        return 204, b""
    return extra_put


def vms_resource(archive: ArchiveResource, server: str, url: str, vars_, objects, wall=None, peers=None,
                 database: str = ":memory:", archives: dict | None = None) -> Resource:
    """The platform's resource for this server with the VMS registered on it, and the
    event database over its tree (created; the process starts it after `restore()`).

    `archives` is {volume: ArchiveResource} on a box with several disks — one tree per
    volume, one recorder per volume (`place_by: volume`). Without it there is one
    volume, it is the whole box, and nothing here is named — and `refresh_volumes`
    replaces that with whatever the operator has declared, once a pass."""
    wall = wall or archive.wall
    volumes = {name: a.root for name, a in (archives or {}).items()} or None
    r = Resource(archive.root, server, url, vars_, objects, archive.bucket_seconds, wall, peers, volumes=volumes)
    r.register("rec", ArchivePolicy(archive, vars_, objects, r.peers, server, volumes=archives))   # footage is the recorder's: rec/<unit>/…, rec/recordings/<unit>
    r.database = EventDatabase(archive.root, server, database, wall, archive.bucket_seconds)
    return r


# Which archives THIS server's resource is responsible for, from the declared rows — the trees whose
# manifests it repairs, whose footage it retains and whose watermark it enforces.
#
# Two kinds and two answers. A LOCAL volume declared for this server is ours by declaration: the disk is
# here whoever is recording into it, and its retention has to run even when no recorder is holding it —
# footage does not stop ageing because nobody is writing. A NETWORK volume is ours only while a recorder
# ON THIS BOX holds it, because exactly one box may sweep a bucket and the hold is what says which.
#
# The holder is an instance string, not a server, so the rec heartbeats are what map the two. A hold whose
# holder nothing is heartbeating for is nobody's — the process is gone and another box will take the
# volume and its policy with it.
def archives_of(vars_, objects, sub, server: str, now: float, lost_after: float = 45.0) -> tuple[dict, dict]:
    """`({volume: path}, {volume: quota_bytes})` for this server's resource."""
    from w2cplatform.console import heartbeats
    from . import volumes as vols_
    on_server = {str(hb.extra.get("instance", "")): str(hb.extra.get("server", ""))
                 for hb in heartbeats(objects, sub.name + "/").values() if now - hb.ts <= lost_after}
    held = vols_.holders(vars_, sub)
    paths, quotas = {}, {}
    for v in vols_.declared(vars_):
        if not v.enabled:
            continue
        if vols_.on_a_box(v):
            mine = v.server == server
        else:
            slot = held.get(v.name)
            mine = slot is not None and not slot.released and now <= slot.until and on_server.get(slot.holder) == server
        if mine:
            paths[v.name], quotas[v.name] = v.url, v.quota_bytes
    return paths, quotas


# Once a pass, before the policy runs: the declared list is configuration and it changes while this
# process is alive. Nothing is torn down — a volume that went away simply stops being walked, and one
# that arrived is created and swept from the next pass. With nothing declared this is a no-op and the
# resource stays the single unnamed tree it was built as.
def refresh_volumes(r: Resource, vars_, objects, sub, now: float) -> dict:
    paths, quotas = archives_of(vars_, objects, sub, r.server, now)
    if not paths:
        return r.volumes
    if paths != r.volumes:
        for p in paths.values():
            os.makedirs(p, exist_ok=True)
        r.volumes, r.root = dict(paths), next(iter(paths.values()))
        r._volume_usage = {}                                   # the numbers belonged to the old set
        hook = r.hooks.get("rec")
        if hook is not None:                                   # the media policy walks the same trees
            hook.volumes = {name: ArchiveResource(hook.res.spool, path, hook.res.bucket_seconds, r.wall)
                            for name, path in paths.items()}
    r.quotas = {k: v for k, v in quotas.items() if v > 0}
    return r.volumes
