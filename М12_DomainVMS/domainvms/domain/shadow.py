"""Lesson 2 — shadow mode: the domain that writes nothing.

The domain computes what the directory WOULD say, observes what the
clusters' workers report, and emits a divergence report. It changes
nothing. The taxonomy, per CAMERA now that the unit of work is a camera on
a worker rather than a recorder with its own database (М9):

    lagging       a worker has not yet applied a camera's revision, within grace     not a fault
    stalled       behind past grace, no progress                                     the real "it did not take effect"
    orphaned      placed by the domain, and no cluster's snapshot lists it            fault
    unmanaged     a cluster runs a camera the domain never placed                     in shadow mode: a MEASUREMENT
    conflict      two clusters — or two workers — claim one camera                   always a fault — placement or fencing
    stale_epoch   a worker reports a camera under a superseded epoch                  the fence catching a writer that should have stopped

The one number is `unmanaged == 0`: anything running that the model does
not describe is a gap in the model, and driving it to zero IS the design
work. Ordering beats equality: "behind by 4 for 40 minutes" is an incident;
"diverged" is an alert you learn to ignore.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WorkerReport:
    """What a worker's heartbeat says: which cameras it runs, under which
    epoch each, and which revision of each it has applied."""
    worker: str
    cluster: str
    server: str
    cameras: list[tuple[str, int, int, int]]   # (camera ref — the domain's name for it, epoch, revision, observed_revision)
    ts: float


@dataclass
class Finding:
    kind: str
    camera: int | None
    where: str | None
    detail: str


@dataclass
class DivergenceReport:
    findings: list[Finding] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for f in self.findings if f.kind == kind)

    @property
    def unmanaged(self) -> int:
        return self.count("unmanaged")

    @property
    def faults(self) -> list[Finding]:
        return [f for f in self.findings if f.kind in ("stalled", "orphaned", "conflict", "stale_epoch")]

    def summary(self) -> str:
        kinds = ("lagging", "stalled", "orphaned", "unmanaged", "conflict", "stale_epoch")
        return "  ".join(f"{k}={self.count(k)}" for k in kinds)


class Shadow:
    """`placed`: camera ref -> cluster the domain's placement says. `epochs`:
    (cluster, ref) -> current epoch from that cluster's vms/epoch/<id>, keyed
    by the ref the snapshot maps it to. `reports`: every worker's heartbeat,
    every cluster. Everything is keyed by the domain's ref: two clusters both
    have an id 1, and the domain never compares by it."""

    def __init__(self, grace_seconds: float = 60.0):
        self.grace = grace_seconds
        self._progress: dict[tuple[str, int], tuple[int, float]] = {}   # (worker, camera) -> (last observed, since when)

    def compare(self, placed: dict, epochs: dict[tuple[str, str], int],
                reports: list[WorkerReport], now: float) -> DivergenceReport:
        rep = DivergenceReport()
        claims: dict[int, list[tuple[str, str]]] = {}
        for r in reports:
            for cam, epoch, revision, observed in r.cameras:
                current = epochs.get((r.cluster, cam), epoch)
                if epoch < current:
                    rep.findings.append(Finding("stale_epoch", cam, f"{r.cluster}/{r.worker}",
                                                f"reports under epoch {epoch}, current is {current}"))
                    continue                              # a fenced writer's claim counts for nothing
                claims.setdefault(cam, []).append((r.cluster, r.worker))
                key = (r.worker, cam)
                last, since = self._progress.get(key, (None, now))
                if last != observed:
                    self._progress[key] = (observed, now); since = now
                behind = revision - observed
                if behind > 0:
                    age = now - since
                    rep.findings.append(Finding("stalled" if age > self.grace else "lagging", cam, f"{r.cluster}/{r.worker}",
                                                f"behind by {behind} revision(s) for {age:.0f}s"))
        for cam, who in claims.items():
            if len(who) > 1:
                rep.findings.append(Finding("conflict", cam, None, f"claimed by {sorted(who)}"))
            elif cam not in placed:
                rep.findings.append(Finding("unmanaged", cam, f"{who[0][0]}/{who[0][1]}", "running, never placed by the domain"))
            elif placed[cam] != who[0][0]:
                rep.findings.append(Finding("conflict", cam, f"{who[0][0]}/{who[0][1]}", f"placed in {placed[cam]}, running in {who[0][0]}"))
        for cam, cl in placed.items():
            if cam not in claims:
                rep.findings.append(Finding("orphaned", cam, cl, "placed, and no worker in the domain runs it"))
        return rep


def reports_from(fed) -> tuple[list[WorkerReport], dict[tuple[str, str], int], list[str]]:
    """Build the reports and the epoch map from every reachable cluster's
    heartbeats and vms/epoch/* — what the shadow job reads each pass."""
    from .federation import Unreachable
    reports, epochs, down = [], {}, []
    for name, c in fed.clusters.items():
        try:
            def ref_of(st):
                return str(st.get("ref") or f"{name}/{st['id']}")
            refs = {}
            for w, hb in c.heartbeats().items():
                cams = []
                for st in hb.get("status", []):
                    if st.get("phase") != "running":
                        continue
                    refs[int(st["id"])] = ref_of(st)
                    cams.append((ref_of(st), int(st.get("epoch", 0)), int(st.get("revision", 0)), int(st.get("observed_revision", 0))))
                reports.append(WorkerReport(w, name, hb.get("server", "?"), cams, float(hb["ts"])))
            for path in c.vars.list("vms/epoch/"):
                items, _ = c.vars.get(path)
                cid = int(path.rsplit("/", 1)[1])
                epochs[(name, refs.get(cid, f"{name}/{cid}"))] = int(items["epoch"])
        except Unreachable:
            down.append(name)
    return reports, epochs, down


def exit_criterion(rep: DivergenceReport, consecutive_clean: int, required: int = 3) -> tuple[bool, str]:
    """The written criterion for switching the domain into write mode."""
    if rep.unmanaged:
        return False, f"unmanaged={rep.unmanaged}: the model does not describe everything that runs"
    if rep.faults:
        return False, f"{len(rep.faults)} fault(s) outstanding: {sorted({f.kind for f in rep.faults})}"
    if consecutive_clean < required:
        return False, f"{consecutive_clean}/{required} consecutive clean reports"
    return True, "unmanaged == 0, no faults, stable across reports: the domain may write"
