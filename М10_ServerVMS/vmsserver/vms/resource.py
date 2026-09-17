"""The resource process — the platform's resource job (w2cplatform.resource)
with the VMS registered on it. One per server, pinned there for as long as
the server exists; on a box it is `python3 -m vms resource`
(`deploy/resource.container`), in М11 the `resource` system job. It has
no controller: it has a policy pass on a timer, a heartbeat, its HTTP, and
the event database over its own tree. What the VMS adds is its part:

    ArchivePolicy   registered as the "rec" hook: the recorder's — repair the manifests, retain media by rec/recordings/<cam>
    vms_routes      GET /manifest/<cam>  the manifest's lines;  GET /segment/<path>  the bytes, Range honoured

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
# The VMS's reads on the resource, plugged into the platform's server: `GET /manifest/<cam>` returns the
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

import os

from w2cplatform.eventdatabase import EventDatabase
from w2cplatform.resource import Resource

from .archive import ArchivePolicy, ArchiveResource, Manifest


def vms_routes(archive: ArchiveResource):
    """The VMS's reads on the resource, plugged into the platform's server."""
    root = archive.root

    def extra(path: str, headers):
        if path.startswith("/manifest/"):
            cam = int(path.rsplit("/", 1)[1])
            return 200, "".join(l for l in Manifest(root, cam)._lines()).encode()
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


def vms_resource(archive: ArchiveResource, server: str, url: str, vars_, objects, wall=None, peers=None,
                 database: str = ":memory:") -> Resource:
    """The platform's resource for this server with the VMS registered on it, and the
    event database over its tree (created; the process starts it after `restore()`)."""
    wall = wall or archive.wall
    r = Resource(archive.root, server, url, vars_, objects, archive.bucket_seconds, wall, peers)
    r.register("rec", ArchivePolicy(archive, vars_))                    # footage is the recorder's: rec/<cam>/…, rec/recordings/<cam>
    r.database = EventDatabase(archive.root, server, database, wall, archive.bucket_seconds)
    return r
