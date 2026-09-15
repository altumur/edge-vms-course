"""One camera's timeline across the resources its recording was written into.

A recording that failed over has footage on two servers: the dead one's until
the failure, the new one's after (the recorder moved; the worker holding the
camera may not have). The console asks every resource that
reports the camera for its manifest and merges. A resource whose heartbeat
is stale is *unreachable*: its ranges are listed from the last thing known
about it — the manifest it served last time, if we cached it, or nothing —
and the answer says so by server. *Unavailable* is a state with a name in
it; *lost* is a word this module never prints for footage on a disk.
"""
from __future__ import annotations

import time
import urllib.request

from vms.archive import Segment


class ManifestReader:
    """How the console gets a manifest from a resource: HTTP, or a fake for tests."""

    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout

    def read(self, url: str, cam: int) -> list[Segment]:
        with urllib.request.urlopen(f"{url}/manifest/{cam}", timeout=self.timeout) as r:
            return [Segment.from_line(l) for l in r.read().decode().splitlines() if l.strip()]


def merged_timeline(resources: dict[str, dict], reader, cam: int, t0: float, t1: float,
                    current_epoch: int | None = None, now: float | None = None, lost_after: float = 45.0) -> dict:
    """`resources` is resources_seen(); returns {segments: [...], unreachable: [server...]}."""
    now = time.time() if now is None else now
    segs, unreachable = [], []
    for server, hb in sorted(resources.items()):
        if str(cam) not in hb.get("units", {}).get("rec", []):     # the platform's heartbeat: units per subsystem — footage is the recorder's tree, rec/<cam>
            continue
        if now - float(hb["ts"]) > lost_after:
            unreachable.append(server)
            continue
        try:
            rows = reader.read(hb["url"], cam)
        except Exception:                        # noqa: BLE001 — the heartbeat is fresh but the server is not answering
            unreachable.append(server)
            continue
        for s in rows:
            if s.end > t0 and s.start < t1:
                segs.append({"start": s.start, "end": s.end, "path": s.path, "epoch": s.epoch, "server": server,
                             "fenced": current_epoch is not None and s.epoch < current_epoch})
    segs.sort(key=lambda d: (d["start"], d["epoch"]))
    return {"segments": segs, "unreachable": unreachable,
            "note": (f"ranges on {', '.join(unreachable)} are unavailable until the server returns — not lost"
                     if unreachable else "")}
