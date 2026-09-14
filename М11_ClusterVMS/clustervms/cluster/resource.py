"""The resource job on a cluster is the platform's (psimplatform.resource):
a `system` job on every server with meta.archive, serving buckets, taking
mirrors from its peers, retaining every subsystem's buckets by that
subsystem's policy. What this module adds is the VMS's part of it:

    ArchivePolicy   registered as the "vms" hook: repair the manifests, close buckets into them, retain media
    vms_routes      GET /manifest/<cam>  the manifest's lines;  GET /segment/<path>  the bytes, Range honoured

Nothing about mirrors, heartbeats or peers is the VMS's, and the names say
so: platform/resources/<server>/heartbeat, platform/mirror, job "resource".
"""
from __future__ import annotations

import os

from vms.archive import ArchivePolicy, ArchiveResource, Manifest  # noqa: F401
from psimplatform.resource import (PeerClient, Resource, mirror_settings, mirrored_buckets, peers_of,  # noqa: F401
                                  resources_seen, serve)


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


def cluster_resource(archive: ArchiveResource, server: str, url: str, vars_, objects, wall=None, peers=None) -> Resource:
    """The platform's resource for this server with the VMS registered on it."""
    r = Resource(archive.root, server, url, vars_, objects, archive.bucket_seconds, wall or archive.wall, peers)
    r.register("vms", ArchivePolicy(archive, vars_))
    return r
