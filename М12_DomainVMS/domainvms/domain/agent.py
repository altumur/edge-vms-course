"""The domain agent: one small Nomad job per cluster whose only right is to
write domain/* in that cluster's Variables — the way a worker's only right
is to write its epochs and its slot, and the controller's is vms/*.

It carries the three things a cluster needs from the domain and nothing
else: the signer's public key set, the revocation list, and the grants for
THIS cluster. When the domain is unreachable it stops updating; the
cluster's console and gateway keep verifying with the keys they have,
issued tokens run to expiry, grants run to theirs, nobody new logs in —
the bounded outage the services table promises, with the mechanism named.
Workers are not involved: nothing about a user reaches a worker, ever.
"""
from __future__ import annotations

import time

from cluster.variables import Variables

from .federation import Unreachable
from .tokens import KeySet, RevocationList

KEYS_PATH, REVOKED_PATH, GRANTS_PATH = "domain/keys", "domain/revoked", "domain/grants"


class DomainPublisher:
    """The signer's side: writes the key set and the revocation list into
    the DOMAIN cluster's Variables, where agents read them."""

    def __init__(self, domain_vars: Variables):
        self.vars = domain_vars

    def publish_keys(self, ks: KeySet) -> None:
        _, idx = self.vars.get(KEYS_PATH)
        self.vars.put(KEYS_PATH, ks.to_items(), cas=idx)

    def publish_revoked(self, rl: RevocationList) -> None:
        _, idx = self.vars.get(REVOKED_PATH)
        self.vars.put(REVOKED_PATH, rl.to_items(), cas=idx)

    def publish_grants(self, cluster: str, grants: list) -> None:
        """The grants for one cluster, under domain/grants/<cluster> in the
        domain cluster's Variables; the cluster's agent copies them home."""
        from .grants import grants_to_items
        path = f"{GRANTS_PATH}/{cluster}"
        _, idx = self.vars.get(path)
        self.vars.put(path, grants_to_items(grants), cas=idx)


class DomainAgent:
    def __init__(self, cluster: str, domain_vars: Variables, cluster_vars: Variables, now=time.time):
        self.cluster, self.domain_vars, self.cluster_vars, self.now = cluster, domain_vars, cluster_vars, now
        self.last_synced: float | None = None
        self.syncs = 0

    def sync(self) -> bool:
        """One pass. False (and nothing written) if the domain did not answer."""
        try:
            keys, _ = self.domain_vars.get(KEYS_PATH)
            revoked, _ = self.domain_vars.get(REVOKED_PATH)
            grants, _ = self.domain_vars.get(f"{GRANTS_PATH}/{self.cluster}")
        except Unreachable:
            return False
        for path, items in ((KEYS_PATH, keys), (REVOKED_PATH, revoked), (GRANTS_PATH, grants)):
            if items is None:
                continue
            have, idx = self.cluster_vars.get(path)
            if have != items:
                self.cluster_vars.put(path, items, cas=idx)
        self.last_synced = self.now()
        self.syncs += 1
        return True


class ClusterTrust:
    """What a cluster's console and gateway read from THEIR OWN cluster's
    Variables — never from the domain — to verify tokens and decide grants
    offline."""

    def __init__(self, cluster_vars: Variables):
        self.vars = cluster_vars

    def keyset(self) -> KeySet | None:
        items, _ = self.vars.get(KEYS_PATH)
        return KeySet.from_items(items) if items else None

    def revoked(self) -> set[str]:
        items, _ = self.vars.get(REVOKED_PATH)
        return RevocationList.from_items(items).jtis

    def grants(self) -> list:
        from .grants import grants_from_items
        items, _ = self.vars.get(GRANTS_PATH)
        return grants_from_items(items)


def main() -> None:
    """python3 -m domain.agent — one per cluster."""
    import os
    import signal
    import threading

    import cluster as _cluster  # noqa: F401  — registers the `nomad://` scheme
    from psimplatform.variables import open_vars

    cluster = os.environ.get("CLUSTER", os.environ.get("NOMAD_REGION", "local"))
    agent = DomainAgent(cluster, open_vars(os.environ["DOMAIN_CONFIG_URL"]),
                        open_vars(os.environ.get("CONFIG_URL") or "nomad://" + os.environ.get("NOMAD_ADDR", "127.0.0.1:4646").replace("http://", "")))
    interval = float(os.environ.get("SYNC_INTERVAL", "30"))
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    while not stop.is_set():
        try:
            ok = agent.sync()
        except Exception:                                    # noqa: BLE001 — the domain is unreachable; keep the last set
            ok = False
        if not ok:
            print(f"{cluster}: domain unreachable; keeping the key set from {agent.last_synced}", flush=True)
        stop.wait(interval)


if __name__ == "__main__":
    main()
