"""Reading an INTERVAL of the archive, and remembering how far you got.

    <archive>/rec/<unit>/manifest.jsonl        what the recorder wrote: the media index
    <archive>/detjob/<job>/manifest.jsonl      what a scan has processed: one line per stretch

The live detector never needed this file. It reads a fan-out with no beginning
and no end, and "where am I" is "now". A scan over recorded footage has both
ends, so it needs the two things the live path never asked for: which segments
cover `[from, to)`, and which of them are already behind it.

Both answers come out of the manifest and nothing else — no Variables, no
heartbeat, no call to another server. That matters for footage from last March:
who currently holds the recording says nothing about who won the race for those
minutes, and the archive may be read on a box where no recorder runs at all.

The progress file is the recorder's manifest again, in shape and for the same
reasons (М10B Lesson 3): append-only, rebuildable from what it describes, and a
crash between the work and the line costs a re-scan of one stretch rather than
a wrong answer.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from w2cplatform.events import unit_dir

from .archive import Manifest, Segment

SUB = "detjob"          # the scan's own tree, beside `rec/` and `vms/` on the same resource
SURVEY = "survey"       # the standing survey's own tree, beside it
MANIFEST = "manifest.jsonl"


# -- the OTHER archive: what a device holds, span by span -------------------------------------------
#
# The summary in the holder's heartbeat (`from`, `to`, `fragments`) answers "is there anything there at
# all". It cannot answer "is there anything at 10:05", and for a device recording on motion the difference
# is most of the day: between its first and its last minute there is mostly nothing.
#
# `None` means the driver cannot list — not that the device holds nothing. A caller that folds the two
# together promises a scan minutes that do not exist, or refuses one that does.
def device_recordings(url: str, t0: float, t1: float, timeout: float = 10.0) -> list[tuple[float, float]] | None:
    import json as _json
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url}?from={t0}&to={t1}", timeout=timeout) as r:
            body = _json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 501:
            return None                                  # this driver cannot list; the summary is all there is
        raise
    return [(float(sp["from"]), float(sp["to"])) for sp in body.get("spans", [])]


# The seconds of `[t0, t1)` a device actually holds. `spans` is what `device_recordings` returned, and the
# `None` case is the caller's to decide: here it means "we cannot tell", and the honest fallback is the
# summary — optimistic, which is right, because being wrong the other way refuses work that would succeed.
def covered_by(spans: list[tuple[float, float]] | None, t0: float, t1: float) -> float:
    if spans is None:
        return max(0.0, t1 - t0)
    return sum(max(0.0, min(b, t1) - max(a, t0)) for a, b in spans)


# One stretch of one segment: the footage, and the part of it this scan asked for.
@dataclass(frozen=True)
class Scan:
    seg: Segment
    t0: float             # unix seconds; the stretch, not the segment — they differ at both ends
    t1: float

    @property
    def seconds(self) -> float:
        return max(0.0, self.t1 - self.t0)

    # Identity in the log. The PATH and the stretch, not the segment alone: a segment can appear twice
    # with two stretches when a newer epoch owns the minutes between them, and a log keyed by path alone
    # would call the second stretch done when only the first was.
    def key(self) -> str:
        return f"{self.seg.path}@{self.t0:.3f}-{self.t1:.3f}"

    # Whether an event the model reports at `ts` belongs to this scan. Decoding starts at the segment's
    # START — a segment opened at 10:00 must be decoded from 10:00 even when the operator asked from
    # 10:05 — so the frames before `t0` are seen, and what they produce is not what was asked for. Without
    # this the answer to "what happened between 10:05 and 10:12" quietly contains 10:00.
    def accepts(self, ts: float) -> bool:
        return self.t0 <= ts < self.t1


# Every stretch of `[t0, t1)` that footage covers, each given to the HIGHEST EPOCH that covers it.
#
# Two epochs overlap in the archive whenever a recorder was fenced with footage in flight: the zombie's
# segments and the survivor's describe the same minutes. Neither "read everything" nor "drop everything
# fenced" is right. Read both and those minutes are scanned twice, so the operator gets every car counted
# twice. Drop every segment belonging to an older epoch and the minutes BEFORE the takeover — which only
# the older epoch ever held, and which are perfectly good footage — disappear from the answer.
#
# So the unit of the decision is the stretch, not the segment: walk the boundaries, and give each
# elementary interval to the highest epoch present there. A segment can come back in two pieces, or in
# none. Adjacent pieces of the same segment are merged back so the caller opens the file once.
def _authoritative(segs: list[Segment], t0: float, t1: float) -> list[Scan]:
    edges = sorted({t0, t1} | {s.start for s in segs} | {s.end for s in segs})
    edges = [e for e in edges if t0 <= e <= t1]
    out: list[list] = []
    for lo, hi in zip(edges, edges[1:]):
        if hi <= lo:
            continue
        covering = [s for s in segs if s.start <= lo and s.end >= hi]
        if not covering:
            continue                                     # a gap: nothing was recorded here, and a scan cannot invent it
        best = max(covering, key=lambda s: s.epoch)
        if out and out[-1][0] == best and out[-1][2] == lo:
            out[-1][2] = hi                              # the same writer either side of a boundary: one stretch
        else:
            out.append([best, lo, hi])
    return [Scan(s, a, b) for s, a, b in out]


# What a scan of `[t0, t1)` on this recording has to read, in order.
def plan(archive_root: str, unit, t0: float, t1: float) -> list[Scan]:
    return _authoritative(Manifest(archive_root, unit).read(), float(t0), float(t1))


# Seconds of `[t0, t1)` the plan actually covers. The operator asked for an hour; if forty minutes were
# recorded, a finished scan that found nothing has to be able to say WHICH — "nothing happened" and
# "nothing was recorded" are different answers and only one of them is about the footage.
def covered(scans: list[Scan]) -> float:
    return sum(s.seconds for s in scans)


class ScanLog:
    """What a scan has already processed: `<archive>/detjob/<job>/manifest.jsonl`,
    one line per stretch, appended AFTER the stretch is scanned and its events are
    written. Durable, so a worker restarting on this server resumes instead of
    starting again; and on the resource rather than in a row, because a worker's
    ACL is `[<name>/epoch/*, <name>/slots/*]` — it may not write configuration."""

    def __init__(self, archive_root: str, job):
        self.path = os.path.join(unit_dir(archive_root, SUB, str(job)), MANIFEST)

    def append(self, scan: Scan, events: int, at: float) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps({"kind": "scan", "key": scan.key(), "path": scan.seg.path, "epoch": scan.seg.epoch,
                                "from": scan.t0, "to": scan.t1, "events": int(events), "at": float(at)}) + "\n")

    def read(self) -> list[dict]:
        try:
            with open(self.path) as f:
                return [json.loads(l) for l in f if l.strip()]
        except FileNotFoundError:
            return []

    def done(self) -> set[str]:
        return {str(d["key"]) for d in self.read()}

    # For the operator's progress, and for nothing else. It is the far end of the furthest stretch
    # recorded, which is NOT a point everything before is finished at: resuming from it would skip a
    # stretch that failed while a later one succeeded. `remaining` is the authoritative answer.
    def done_through(self) -> float:
        return max((float(d["to"]) for d in self.read()), default=0.0)

    def events(self) -> int:
        return sum(int(d.get("events", 0)) for d in self.read())


# Moments into stretches: what to keep when the reason for keeping it is that a model fired.
#
# An event is an instant and footage is an interval, so something has to turn one into the other, and the
# three numbers are all decisions rather than tuning. `pre` and `post` are what makes the clip watchable —
# a car crossing the line at 10:04:31 is useless as a one-second file and obvious as a twenty-second one.
# `join` is what keeps a busy minute from becoming three hundred two-second clips: hits closer together
# than that are one stretch, and the gap between them is cheaper to keep than to cut out.
#
# `watched` clips the result to what was actually looked at — padding runs off the end of a span otherwise,
# and asks for minutes the device never recorded.
def hit_spans(times: list[float], watched: list[tuple[float, float]],
              pre: float, post: float, join: float) -> list[tuple[float, float]]:
    if not times:
        return []
    raw = sorted((t - pre, t + post) for t in times)
    merged: list[list[float]] = []
    for a, b in raw:
        if merged and a - merged[-1][1] <= join:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = []
    for a, b in merged:
        for wa, wb in watched:
            lo, hi = max(a, wa), min(b, wb)
            if hi > lo:
                out.append((lo, hi))
    return sorted(out)


class Frontier:
    """How far a standing survey has watched: one number, durable, beside its events.

    The same argument as `ScanLog` and the same place for the same reason — a worker
    may not write configuration — but a different shape, because the work is different.
    A scan has a plan and ticks stretches off it; a survey has no end, and what it
    keeps is a moving edge."""

    def __init__(self, archive_root: str, unit):
        self.path = os.path.join(unit_dir(archive_root, SURVEY, str(unit)), "frontier.json")

    def read(self) -> float | None:
        try:
            with open(self.path) as f:
                return float(json.load(f)["watched_through"])
        except (FileNotFoundError, ValueError, KeyError):
            return None                                  # never started, or the file was lost: the row decides where to begin

    # Written after the events of that stretch, and atomically: a crash between the work and the number
    # costs a re-watch, a half-written number would cost the frontier itself.
    def set(self, t: float) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"watched_through": float(t)}, f)
        os.replace(tmp, self.path)


# The plan minus what the log says is behind us — what a restarted worker picks up.
def remaining(scans: list[Scan], log: ScanLog) -> list[Scan]:
    done = log.done()
    return [s for s in scans if s.key() not in done]
