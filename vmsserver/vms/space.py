"""What the archive does when the disk is full — the recorder's answer to the
resource's one question, "free N bytes".

Retention by days is a promise to the operator: thirty days of camera 7. This
module is what happens when the promise cannot be kept, and it is deliberately
not the same thing. Three steps, in this order:

    1. give up what is not ours    a unit whose recorder now writes on another server: send it there
    2. cut above the floor         from the unit with the most days over `min_days`, oldest first
    3. say the shortfall out loud  everything on the floor and still no room: a number, not a quiet cut

Step 1 is why there is no separate "evacuation" job, schedule or button. A
recording written here while the owner's server was down is not lost, not
wrong, and not urgent — the console already merges timelines across resources.
It becomes work only when the disk it sits on needs the space, and then the
server that needs it is the one that acts.

The pressure is measured by `w2cplatform.resource` (the disk, not the tree);
what to give up is decided here, because only the subsystem knows what its
files mean. Nothing in the platform deletes a segment.
"""
from __future__ import annotations

import os

from w2cplatform.console import holder_of
from w2cplatform.contract import draining
from w2cplatform.resource import resources_seen

from .archive import SUB, ArchiveResource, Manifest

MAX_SEGMENTS = 50          # one pass moves a batch, not an archive: the next pass continues
ROOM_MARGIN = 0.9          # never fill the destination's last tenth — that is its own watermark's air


# How many days of footage a unit has here, from the manifest — the operator's real answer to "how far
# back does camera 7 go", which retention_days only promises.
def depth_days(archive: ArchiveResource, unit: str, now: float) -> float:
    segs = Manifest(archive.root, unit).read()
    return (now - min(s.start for s in segs)) / 86400 if segs else 0.0


# What a unit occupies here — from the manifest's `bytes`, not from the disk. Every line already carries
# the size, so the answer costs a read of one file instead of a walk of the tree.
def unit_bytes(archive: ArchiveResource, unit: str) -> int:
    return sum(s.bytes for s in Manifest(archive.root, unit).read())


# Units on this disk whose recorder is now on ANOTHER server -> that server. No event, no outage journal,
# no "recovery mode": what is on the disk (`units()`) against who holds it (the heartbeats). The same
# comparison answers the operator's "where is camera 7's footage" and drives the evacuation below.
def foreign(archive: ArchiveResource, objects, server: str, now: float, lost_after: float = 45.0) -> dict[str, str]:
    """{unit: the server whose recorder writes it now} for units held elsewhere."""
    out = {}
    for unit in archive.units():
        found = holder_of(objects, SUB + "/", unit, now, lost_after)
        if found is None:
            continue                                            # nobody holds it: not ours to send anywhere
        where = found[1].extra.get("server")
        if where and where != server:
            out[unit] = where
    return out


# Step 1. Push a bounded batch of a foreign unit's segments to the server that writes it now, then delete
# locally only what that server's own manifest confirms it has. The deletion follows an observed fact, not
# a 204: a copy that never arrived is a copy we still hold.
#
# The destination's free space is read from its heartbeat FIRST. Evacuating onto a disk that is itself
# tight moves the problem and invites the pair to trade gigabytes back and forth; a destination with no
# room is skipped, and step 2 answers instead.
def evacuate(archive: ArchiveResource, objects, peers, server: str, need: int, now: float,
             lost_after: float = 45.0, max_segments: int = MAX_SEGMENTS, vars_=None) -> dict:
    """Send foreign units home, delete what the destination confirms."""
    seen, freed, moved, skipped = resources_seen(objects), 0, 0, {}
    drains = draining(vars_) if vars_ is not None else ""
    for unit, to in sorted(foreign(archive, objects, server, now, lost_after).items()):
        if freed >= need:
            break
        hb = seen.get(to)
        if hb is None or now - float(hb["ts"]) > lost_after:
            skipped[unit] = f"{to} silent"
            continue
        if to == drains:                                         # about to stop: do not hand it gigabytes first
            skipped[unit] = f"{to} draining"
            continue
        room = float(hb.get("space", {}).get("free", 0)) * ROOM_MARGIN
        man = Manifest(archive.root, unit)
        segs = sorted(man.read(), key=lambda s: (s.start, s.epoch))
        sent, size = [], 0
        for s in segs:
            if freed + size >= need or len(sent) >= max_segments:
                break
            if size + s.bytes > room:
                skipped[unit] = f"{to} has no room"
                break
            try:
                with open(os.path.join(archive.root, s.path), "rb") as f:
                    peers.put_raw(hb["url"], f"segment/{s.path}", f.read(), {"X-Segment": s.line()})
            except (OSError, IOError) as e:                      # noqa: BLE001 — a peer that stopped answering mid-batch
                skipped[unit] = str(e)
                break
            sent.append(s); size += s.bytes
        if not sent:
            continue
        there = confirmed(peers, hb["url"], unit)
        keep, gone = [], 0
        for s in segs:
            if s in sent and s.path in there:
                try:
                    os.remove(os.path.join(archive.root, s.path))
                except FileNotFoundError:
                    pass
                gone += s.bytes; moved += 1
            else:
                keep.append(s)
        if gone:
            man.rewrite(keep)
            freed += gone
    return {"freed": freed, "moved": moved, **({"skipped": skipped} if skipped else {})}


# What the destination says it has: the paths in ITS manifest for this unit. A read, over the same route
# the console uses to draw a timeline — nothing was added for the sake of the check.
def confirmed(peers, url: str, unit: str) -> set[str]:
    try:
        body = peers.get_raw(url, f"manifest/{unit}").decode()
    except (OSError, IOError):                                   # unreachable now: confirm nothing, delete nothing
        return set()
    from .archive import Segment
    return {Segment.from_line(l).path for l in body.splitlines() if l.strip()}


# Step 2. Cut from whoever has the most days over the floor, oldest segment first, one at a time so the
# choice is made again after every deletion — the unit that was deepest stops being deepest, and the loss
# spreads instead of falling on one camera.
#
# Not "the oldest segments on the resource": that empties the camera with the longest retention, which is
# the one the operator cared most about. Not "the biggest file": that empties the camera with the highest
# bitrate, which is usually the same camera.
def cut(archive: ArchiveResource, need: int, now: float, min_days: float) -> dict:
    """Free `need` bytes from the unit with the most slack over the floor."""
    freed, removed = 0, 0
    while freed < need:
        best, slack = None, 0.0
        for unit in archive.units():
            over = depth_days(archive, unit, now) - min_days
            if over > slack:
                best, slack = unit, over
        if best is None:
            break                                                # everything is on the floor
        man = Manifest(archive.root, best)
        segs = sorted(man.read(), key=lambda s: (s.start, s.epoch))
        if not segs:
            break
        s = segs[0]
        try:
            os.remove(os.path.join(archive.root, s.path))
        except FileNotFoundError:
            pass
        man.rewrite(segs[1:])
        freed += s.bytes; removed += 1
    return {"freed": freed, "removed": removed}
