"""Lesson 3 — the API, and what it refuses.

The domain console's write API is a façade: an edit goes to the directory
("where is camera 7"), then to the OWNING CLUSTER's console — the one in
front of that cluster's controller, the only writer of its vms/* — and that
cluster's grants decide. The domain console owns nothing and never writes
a camera row on its own account; a create goes to the cluster the
placement service chose, and that cluster's controller places it on a
worker (М11 Lesson 10). The domain never names a worker or a server.

    idempotency keys    a retried PUT is the same PUT, not a second edit
    what it refuses     a client may not set placement at either level (cluster, worker, server), nor what
                        a worker observes (phase, observed_revision) or takes (epoch)
    positions/reasons   the read model's `phase` and `position` are passed through untouched
    authentication      Lesson 3 ships it unauthenticated and says so; Lesson 4 adds `verifier`
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from .federation import DomainDirectory, Unreachable

FORBIDDEN_FIELDS = ("cluster", "worker", "server", "placement", "epoch", "observed_revision", "phase", "revision")


class ClusterConsole(Protocol):
    """What the domain can ask of a cluster's console (М11 Lesson 10): the same
    two writes its controller offers, with the caller's subject for its grants."""
    def update_camera(self, camera: int, fields: dict, subject: str | None) -> dict: ...
    def create_camera(self, fields: dict, subject: str | None) -> dict: ...


@dataclass
class ApiError(Exception):
    status: int
    detail: str

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}"


class ConsoleAPI:
    def __init__(self, directory: DomainDirectory, consoles: Callable[[str], ClusterConsole],
                 verifier: Callable[[str], str] | None = None, pending=None, last_known=None):
        """`consoles(cluster)` finds that cluster's console — its Nomad service,
        in production; a dict in tests. `verifier(token) -> subject` is Lesson 4;
        None means unauthenticated, and the API says so on every response.

        `pending` and `last_known` are Lesson 9: somewhere to keep an edit for a cluster
        that is off (`PendingEdits`), and what the domain last saw of the camera
        (`ReadView.last_known`). Without them the API answers as Lesson 3 did — 503,
        could not look — rather than pretend to keep an edit it has nowhere to put."""
        self.directory, self.consoles, self.verifier = directory, consoles, verifier
        self.pending, self.last_known = pending, last_known
        self._seen: dict[str, dict] = {}                      # idempotency key -> response

    def _subject(self, token: str | None) -> str | None:
        if self.verifier is None:
            return None
        if not token:
            raise ApiError(401, "a token is required")
        return self.verifier(token)

    def _refuse_placement(self, fields: dict) -> None:
        bad = [k for k in fields if k in FORBIDDEN_FIELDS]
        if bad:
            raise ApiError(400, f"a client may not set {bad}: the domain places on a cluster and the cluster's "
                                f"controller places on a worker, each with a stored reason; epoch, phase and "
                                f"observed_revision are the worker's; revision is the controller's")

    def update_camera(self, camera: int, fields: dict, idempotency_key: str, token: str | None = None) -> dict:
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]                # the same PUT, not a second edit
        self._refuse_placement(fields)
        subject = self._subject(token)
        ans = self.directory.where(camera)
        if not ans.found:
            kept = self._keep(camera, fields, subject, ans)
            if kept is not None:
                self._seen[idempotency_key] = kept
                return kept
            raise ApiError(404 if ans.complete else 503, ans.sentence())
        try:
            result = self.consoles(ans.cluster).update_camera(camera, fields, subject)
        except Unreachable:
            # It answered the directory and not the edit — gone between the two, or, with a directory
            # answered from memory (Lesson 11), gone since the last pass. Either way the owner is known,
            # and silent: the same case as Lesson 9's.
            kept = self._keep_for(ans.cluster, camera, fields, subject)
            if kept is None:
                raise ApiError(503, f"{ans.cluster} did not answer the edit")
            self._seen[idempotency_key] = kept
            return kept
        resp = {"camera": camera, "cluster": ans.cluster, "worker": ans.worker, "result": result,
                "authenticated": self.verifier is not None}
        self._seen[idempotency_key] = resp
        return resp

    # Lesson 9. The camera was not found because its cluster did not answer — and the domain knows which cluster
    # that was, from what it last saw. Keep the edit for it instead of refusing: per field, against the value
    # last seen. Only then, though: a camera missing from a cluster that DID answer is gone, not waiting, and
    # a complete answer that found nothing is a 404. Accepted is not applied, and the response says which.
    def _keep(self, camera, fields: dict, subject: str | None, ans) -> dict | None:
        if self.pending is None or self.last_known is None or ans.complete:
            return None
        known = self.last_known(camera)
        if known is None or known[0] not in ans.unreachable:
            return None
        return self._keep_for(known[0], camera, fields, subject)

    def _keep_for(self, cluster: str, camera, fields: dict, subject: str | None) -> dict | None:
        if self.pending is None or self.last_known is None:
            return None
        known = self.last_known(camera)
        if known is None or known[0] != cluster:
            return None
        row = known[1]
        entry = self.pending.add(cluster, camera, fields, row, subject)
        return {"camera": camera, "cluster": cluster, "pending": True, "fields": entry["fields"],
                "detail": f"{cluster} is not answering; the edit is kept and will be applied when it is back",
                "authenticated": self.verifier is not None}

    def create_camera(self, fields: dict, cluster: str, idempotency_key: str, token: str | None = None) -> dict:
        """`cluster` comes from the placement service's stored decision, which
        the console reads and forwards — it does not choose. The worker is the
        cluster controller's decision, returned, never sent."""
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]
        self._refuse_placement(fields)
        subject = self._subject(token)
        result = self.consoles(cluster).create_camera(fields, subject)
        resp = {"cluster": cluster, "result": result, "authenticated": self.verifier is not None}
        self._seen[idempotency_key] = resp
        return resp
