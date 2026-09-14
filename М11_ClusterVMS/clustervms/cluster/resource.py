"""The resource job on a cluster is М10's resource process (vms.resource) run as
a `system` job on every server with meta.archive: the platform's Resource with
the VMS registered on it — serving buckets, taking mirrors from its peers,
retaining every subsystem's buckets by that subsystem's policy, and keeping
the event database over its own tree. Nothing here is new; the names say so:
platform/resources/<server>/heartbeat, platform/mirror, job "resource".

    cluster_resource   = vms.resource.vms_resource: ArchivePolicy registered as the "vms" hook, an EventDatabase attached
    vms_routes         GET /manifest/<cam>  the manifest's lines;  GET /segment/<path>  the bytes, Range honoured
"""
from __future__ import annotations

from vms.archive import ArchivePolicy, ArchiveResource, Manifest  # noqa: F401
from vms.resource import vms_resource as cluster_resource, vms_routes  # noqa: F401
from psimplatform.resource import (PeerClient, Resource, mirror_settings, mirrored_buckets, peers_of,  # noqa: F401
                                  resources_seen, serve)
