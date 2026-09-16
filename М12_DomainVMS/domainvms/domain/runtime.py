"""Wiring for the real processes: the environment the jobspecs set, turned
into Clusters over the config store (`open_vars`) and the object store. Not exercised by the
tests (no Nomad here); everything it wires is."""
from __future__ import annotations

import os

import cluster as _cluster  # noqa: F401  — registers the `nomad://` scheme with the platform
from cluster.objectstore import open_store
from psimplatform.variables import open_vars

from .federation import Cluster, Federation


def federation_from_env(var: str = "CLUSTERS") -> Federation:
    """CLUSTERS=north=nomad://nomad.north:4646|http://minio.north:9000/x,south=...
    The first entry, or DOMAIN_CLUSTER, is the domain cluster.

    Each half is a URL with a scheme: the config store (`open_vars` — `nomad://`,
    `file://`, whatever a backend registered) and the object store (`open_store`).
    Neither half names a vendor in code; a cluster on another orchestrator is a
    different scheme in this one string."""
    fed = Federation()
    domain = os.environ.get("DOMAIN_CLUSTER")
    for i, entry in enumerate(filter(None, os.environ.get(var, "").split(","))):
        name, rest = entry.split("=", 1)
        config_url, objects = rest.split("|", 1)
        fed.add(Cluster(name, open_vars(config_url), open_store(objects),
                        is_domain_cluster=(name == domain) if domain else i == 0))
    if not fed.clusters:
        raise SystemExit(f"{var} is empty: name at least one cluster")
    return fed
