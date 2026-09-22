"""The resource — a platform job, one per server, pinned there for as long
as the server exists. It knows the shape of what every subsystem leaves on
a server's disks and nothing about what it means:

    <root>/<subsystem>/<unit>/e<epoch>/...          each subsystem's tree: its buckets, and whatever else it
                                                    keeps beside them (the VMS: media and a manifest — its own)
    <root>/.mirror/<server>/<subsystem>/<unit>/...  copies of another server's closed buckets (the knob)

    platform/resources/<server>/heartbeat   {server, ts, url, usage, space: {total, free}, units: {sub: [unit]}, mirrors: {server: n}}
    platform/mirror                         the knob: {enabled, copies}
    platform/space                          the knob: {enabled, high, low, min_days} — the disk's watermark
    <sub>/retention, <sub>/retention/<unit> {days}: each subsystem's policy for its buckets, written by ITS controller

    GET  <url>/buckets/<sub>/<unit>    closed buckets, from the files
    GET  <url>/events/<path>           one bucket (also .mirror/<server>/<path>)
    GET  <url>/mirrored/<server>       which of <server>'s buckets this server holds copies of
    GET  <url>/events?from&to&cam&kind&subsystem&unit   this resource's EventDatabase: its buckets and its copies
    PUT  <url>/mirror/<server>/<path>  another resource leaves a copy of one of ITS closed buckets here

The policy pass runs on a timer: retain each subsystem's buckets by its
policy; relieve the disk if it is over the high mark — the resource measures
and says how many bytes to free, each subsystem decides what to give up;
mirror closed buckets to the next live resource(s) after this one
in sorted order — nobody assigns peers, the rule is the assignment; and any
subsystem-specific pass a subsystem registered (the VMS registers its
media repair and retention). `restore` is the reverse of mirror, run by
the owner at start: a server back with an empty disk pulls its buckets
home. No controller is involved in any of it.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # resource.py — the resource as a platform job: heartbeat, buckets over HTTP, retention by each
# subsystem's
# row, mirror to a peer, restore
#
# **Role in the module.** Lesson 3. One resource per server, pinned there for as long as the server exists.
# It knows the shape of what every subsystem leaves on the server's disks —
# `<root>/<subsystem>/<unit>/e<epoch>/...` — and nothing about what it means; the VMS keeps media and a
# manifest beside its buckets and the resource neither reads nor names them. It writes its own heartbeat
# object (`platform/resources/<server>/heartbeat`), serves buckets over HTTP, and runs a policy pass on a
# timer: each subsystem's registered hook first (the VMS registers repair, close and media retention via
# `Resource.register`), then bucket retention by each subsystem's own `<sub>/retention[/<unit>]` row, then
# the mirror. Mirroring is a knob (`platform/mirror`), and peers are chosen by a rule — the next `copies`
# live resources after mine in sorted order — so nobody assigns them. `restore` is the reverse, run by the
# owner at start. No controller is involved in any of it. `eventdatabase.EventDatabase` is the database the
# job runs over this tree (`Resource.database`), served as `GET /events` and told by `retain` what it removed;
# `console.py` reads `resources_seen`.
#
# ## Module-level names
# - `MIRROR_DIR = ".mirror"` — under a resource root, `.mirror/<server>/<sub>/<unit>/e<epoch>/…` holds
#   copies of another server's closed buckets. Hidden so `subsystems_under` never counts it as this server's
#   data.
# - `MIRROR_KEY = "platform/mirror"` — the Variable `{enabled, copies}`.
# - `RESOURCES = "platform/resources"` — the object-store prefix for resource heartbeats.
#
# ### `__init__(self, root, server, url, vars_, objects, bucket_seconds=600, wall=time.time, peers=None,
# lost_after=45.0)` `root` is the tree (created), `server` the name that goes into heartbeats and peer
# selection, `url` how others reach this resource's HTTP. `hooks` starts empty. `lost_after` is how old a
# peer's heartbeat may be to count as live.
#
# ## Notes
# - The heartbeat's `units` and `mirrors` are derived from the tree on every call — the resource keeps no
#   state a restart could lose.
# - `retain` and `mirror` both walk the tree each pass; on one box that is cheap, and it keeps the job
#   stateless.
# - The console's `/resources` route is `resources_seen` with a `live | silent` label by `lost_after`;
#   `/metrics` counts `<sub>_resources_live` the same way.
# ================================================================================================
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .contract import BUILD, SCHEMA, check_schema
from .events import Bucket, buckets_under, parse_bucket, subsystems_under

MIRROR_DIR = ".mirror"
MIRROR_KEY = "platform/mirror"
SPACE_KEY = "platform/space"
RESOURCES = "platform/resources"


# The disk under `root`, not the tree on it: `f_bavail` and not `f_bfree`, because reserved blocks are not
# ours to spend. A test cannot fill a disk, so the probe is a seam — `Resource(space_probe=...)`.
def disk_space(root: str) -> tuple[int, int]:
    """(total, free) bytes of the filesystem `root` is on."""
    # `shutil.disk_usage` and not `os.statvfs`, which does not exist on Windows. This is the happy case of
    # a portability problem: the standard library already had the seam, and it keeps the meaning we want on
    # both sides — POSIX `free` is `f_bavail * f_frsize`, exactly the line this replaced, and Windows
    # `free` is GetDiskFreeSpaceExW's "available to the caller", which is the same idea (what is ours to
    # spend) rather than the volume's own free space. The Go port has to write both by hand
    # (`w2cplatform/space_unix.go`, `space_windows.go`); here it is one call.
    u = shutil.disk_usage(root)
    return u.total, u.free


# Parses the JSON line `Bucket.line()` produced (types coerced back). Used by `PeerClient`.
def bucket_from_line(line: str) -> Bucket:
    d = json.loads(line)
    return Bucket(d["subsystem"], str(d["unit"]), int(d["epoch"]), float(d["start"]), float(d["end"]), d["path"], int(d["events"]))


# Reads the watermark. `high` and `low` are USED fractions of the disk: over `high` the resource starts
# freeing and stops at `low`, and the gap between them is the whole point — one mark alone gives a saw, a
# file freed and a file written, for ever. Choose the gap in HOURS OF INGEST, not in percent: fifty cameras
# at four megabit write about 2.2 TB a day, and ten percent of a 20 TB disk is less than one of them.
# `min_days` is the floor no unit is cut below; when everything is on the floor the answer is a shortfall,
# said out loud, and not a quiet cut into yesterday.
def space_settings(vars_) -> dict:
    items, _ = vars_.get(SPACE_KEY)
    d = items or {}
    return {"enabled": bool(items) and d.get("enabled") == "true",
            "high": float(d.get("high", 0.85)), "low": float(d.get("low", 0.75)),
            "min_days": float(d.get("min_days", 3))}


# Reads the knob: `enabled` is true only if the row exists and says `"true"`; `copies` defaults to 1.
def mirror_settings(vars_) -> dict:
    items, _ = vars_.get(MIRROR_KEY)
    return {"enabled": bool(items) and items.get("enabled") == "true", "copies": int((items or {}).get("copies", 1))}


# The unit's days if its subsystem set `<sub>/retention/<unit>`, else the subsystem's `<sub>/retention`,
# else a year. For the VMS the per-unit row is the derived row `vms/retention/<id>` written by
# `SpecController._derived` from `events_retention_days`; on delete it becomes `{days: 0}` so the buckets go
# on the next pass. Each subsystem's controller owns its row; the resource only reads.
def retention_days(vars_, subsystem: str, unit: str, default: float = 365.0) -> float:
    """The unit's days if its subsystem set them, else the subsystem's, else a year."""
    for path in (f"{subsystem}/retention/{unit}", f"{subsystem}/retention"):
        items, _ = vars_.get(path)
        if items and "days" in items:
            return float(items["days"])
    return default


# The rule that replaces a map: sort the other live servers, take those after mine then wrap around, and
# keep the first `copies`. `test_the_resource_is_a_platform_job…`: with `srv-a, srv-b, srv-c`, `srv-a`'s
# peer is `srv-b` and `srv-c`'s is `srv-a`.
def peers_of(server: str, live: list[str], copies: int) -> list[str]:
    """The rule that replaces a map: the next `copies` live resources after mine, in sorted order."""
    others = sorted(s for s in live if s != server)
    if not others:
        return []
    after = [s for s in others if s > server] + [s for s in others if s < server]
    return after[:copies]


# Every resource heartbeat under `platform/resources/`, keyed by `server`, whatever its age. Callers filter
# by `ts`.
def resources_seen(objects) -> dict[str, dict]:
    out = {}
    for key in objects.list(RESOURCES + "/"):
        if key.endswith("/heartbeat"):
            raw = objects.get(key)
            if raw:
                hb = json.loads(raw)
                out[hb["server"]] = hb
    return out


# The server names present under `<root>/.mirror/`.
def mirrored_servers(root: str) -> list[str]:
    try:
        return sorted(d for d in os.listdir(os.path.join(root, MIRROR_DIR)) if os.path.isdir(os.path.join(root, MIRROR_DIR, d)))
    except FileNotFoundError:
        return []


# Copies this resource holds of `server`'s buckets, parsed relative to `.mirror/<server>` so `path` is the
# original path on `server`. Line counts are taken from the copy.
def mirrored_buckets(root: str, server: str, bucket_seconds: int = 600) -> list[Bucket]:
    """Copies this resource holds of <server>'s buckets; `path` is the ORIGINAL path on <server>."""
    base = os.path.join(root, MIRROR_DIR, server)
    out = []
    for d, _, files in os.walk(base):
        for f in files:
            p = os.path.join(d, f)
            parsed = parse_bucket(p, base)
            if parsed:
                sub, unit, epoch, start = parsed
                with open(p) as fh:
                    n = sum(1 for l in fh if l.strip())
                out.append(Bucket(sub, unit, epoch, start, start + bucket_seconds, os.path.relpath(p, base), n))
    return sorted(out, key=lambda b: (b.start, b.epoch))


# How one resource talks to another: HTTP. Tests substitute an in-process client with the same three methods
# over directories.
class PeerClient:
    """How one resource talks to another: HTTP; tests substitute an in-process client."""
    def __init__(self, timeout: float = 5.0): self.timeout = timeout

    # `GET <url>/mirrored/<server>` — which of `server`'s buckets the peer already holds.
    def mirrored(self, url: str, server: str) -> list[Bucket]:
        with urllib.request.urlopen(f"{url}/mirrored/{server}", timeout=self.timeout) as r:
            return [bucket_from_line(l) for l in r.read().decode().splitlines() if l.strip()]

    # `PUT <url>/mirror/<server>/<path>` with the bucket's bytes; anything but 200/201/204 raises `IOError`.
    def put(self, url: str, server: str, path: str, data: bytes) -> None:
        req = urllib.request.Request(f"{url}/mirror/{server}/{path}", data=data, method="PUT")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.status not in (200, 201, 204):
                raise IOError(f"PUT mirror {path}: {r.status}")

    # `PUT <url>/<path>` with arbitrary bytes and headers — the door a subsystem's own PUT route goes
    # through. The platform does not know what is being sent; it knows how to send it.
    def put_raw(self, url: str, path: str, data: bytes, headers: dict | None = None) -> int:
        req = urllib.request.Request(f"{url}/{path.lstrip('/')}", data=data, method="PUT", headers=headers or {})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.status not in (200, 201, 204):
                raise IOError(f"PUT {path}: {r.status}")
            return r.status

    # `GET <url>/<path>` with arbitrary bytes back — the reading half of the same door.
    def get_raw(self, url: str, path: str) -> bytes:
        with urllib.request.urlopen(f"{url}/{path.lstrip('/')}", timeout=self.timeout) as r:
            return r.read()

    # `GET <url>/events/.mirror/<server>/<path>` — pull a copy back (restore).
    def get(self, url: str, server: str, path: str) -> bytes:
        with urllib.request.urlopen(f"{url}/events/{MIRROR_DIR}/{server}/{path}", timeout=self.timeout) as r:
            return r.read()


# One server's resource: its tree, its heartbeat, its policy pass.
# A subsystem's `free` is asked with the volume that is short, and older hooks do not take one. Both are
# right: a subsystem whose files are all on one disk has nothing to choose, and one that spreads them does.
# The resource asks the richer way first and falls back, rather than making every subsystem change on the
# day the second disk arrives.
def _ask_to_free(free, need: int, now: float, min_days: float, volume: str) -> dict:
    try:
        return free(need, now, min_days, volume=volume)
    except TypeError:
        return free(need, now, min_days)


class Resource:
    """One server's resource: its tree, its heartbeat, its policy pass."""

    def __init__(self, root: str | None, server: str, url: str, vars_, objects, bucket_seconds: int = 600,
                 wall=time.time, peers: PeerClient | None = None, lost_after: float = 45.0, space_probe=None,
                 volumes: dict[str, str] | None = None):
        # A server's disks, named. One volume is the common case and stays the whole of `root`; several are
        # what a box with more than one disk actually has, and they are the resource's INTERNAL structure:
        # the resource is still one per server, because reachability is a property of a server and a volume
        # has no address. What a volume does have is its own bottom — which is why the watermark, the only
        # thing here that ever measured a disk, becomes a loop over them.
        self.volumes = dict(volumes) if volumes else {"default": root}
        if not self.volumes or any(not v for v in self.volumes.values()):
            raise ValueError("a resource needs at least one volume with a path")
        self.root = next(iter(self.volumes.values()))          # the first: what single-volume callers still mean
        self.server, self.url, self.vars, self.objects = server, url, vars_, objects
        self.bucket_seconds, self.wall, self.peers, self.lost_after = bucket_seconds, wall, peers or PeerClient(), lost_after
        check_schema(vars_)                                  # a build older than the store does not run at all
        self.space_probe = space_probe or disk_space         # a test cannot fill a disk
        self.last_usage: int | None = None                   # the tree walk's answer, refreshed by `pass_`
        self.usage_at = 0.0                                  # …and when it was taken: a stale number must say so
        self.hooks: dict[str, object] = {}         # subsystem -> object with .pass_(now) -> dict: its own policy on ITS part of the tree
        self.database = None                       # an eventdatabase.EventDatabase over this tree, if the job runs one: served as GET /events
        for path in self.volumes.values():
            os.makedirs(path, exist_ok=True)

    # -- the volumes ---------------------------------------------------------------------
    # Which volume holds a unit is not written down anywhere: the unit's directory IS the answer, exactly
    # as `subsystems_under` already derives what is on this resource at all. A map would be a second truth
    # about the disks, and it would drift.
    def volume_of(self, sub: str, unit: str) -> str | None:
        for name, path in self.volumes.items():
            if os.path.isdir(os.path.join(path, sub, str(unit))):
                return name
        return None

    # Where a unit that is not here yet should go: the emptiest volume. Called when something writes for
    # the first time; after that `volume_of` answers, and a unit does not move between disks — that would
    # be copying terabytes as a side effect of a pass.
    def place_volume(self) -> str:
        best, most = next(iter(self.volumes)), -1
        for name, path in self.volumes.items():
            _, free = self.space_probe(path)
            if free > most:
                best, most = name, free
        return best

    # The absolute path of something named relative to a volume: the volume that has it, else the emptiest.
    def path_of(self, rel: str, volume: str | None = None) -> str:
        if volume is not None:
            return os.path.join(self.volumes[volume], rel)
        for path in self.volumes.values():
            full = os.path.join(path, rel)
            if os.path.exists(full):
                return full
        return os.path.join(self.volumes[self.place_volume()], rel)

    # A subsystem installs an object with `pass_(now) -> dict` for its own part of the tree — the same "code
    # under a name" door `spec.register_constraint` opens.
    def register(self, subsystem: str, hook) -> None:
        self.hooks[subsystem] = hook

    # -- what is here -------------------------------------------------------------------
    # `subsystems_under(root)` — what is here, from the directories.
    def units(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for path in self.volumes.values():
            for sub, units in subsystems_under(path).items():
                out.setdefault(sub, []).extend(u for u in units if u not in out.get(sub, []))
        return {k: sorted(v) for k, v in out.items()}

    # Every bucket of every unit whose `end <= now`. Only these are mirrored.
    def closed_buckets(self) -> list[Bucket]:
        out = []
        for sub, units in self.units().items():
            for unit in units:
                for path in self.volumes.values():
                    out += [b for b in buckets_under(path, sub, unit, self.bucket_seconds) if b.end <= self.wall()]
        return out

    # Total bytes under `root` — every file, not only the ones some subsystem accounts for. A walk, and
    # therefore NOT something to do on a timer: at fifty cameras and ten-minute segments a month of
    # archive is a quarter of a million files, and walking them touches every inode in the tree. Measured
    # once per policy pass (`pass_`), published from the cache with the time it was taken (`usage_at`).
    # What decides anything is `space()` — one `statvfs`, cheap enough for every heartbeat.
    def usage(self) -> int:
        total = 0
        for root in self.volumes.values():
            for d, _, files in os.walk(root):
                for f in files:
                    total += os.path.getsize(os.path.join(d, f))
        return total

    # The cached number, measured now if it never was: the first heartbeat of a process pays for it once.
    def usage_cached(self) -> int:
        if self.last_usage is None:
            self.last_usage, self.usage_at = self.usage(), self.wall()
        return self.last_usage

    # (total, free) of the disk, and how full it is. `free` is what a peer reads before sending anything
    # here: an evacuation onto a disk that is itself tight only moves the problem.
    def space(self, volume: str | None = None) -> dict:
        """One volume's disk, or every volume summed when none is named. The sum
        is what a fleet view wants; what DECIDES anything is a single volume —
        a box 50% full across two disks with one of them at 98% is full."""
        names = [volume] if volume is not None else list(self.volumes)
        total = free = 0
        for n in names:
            t, f = self.space_probe(self.volumes[n])
            total += t; free += f
        return {"total": total, "free": free, "used": total - free,
                "full": (total - free) / total if total else 0.0}

    # Every volume by name, which is what the console shows and what `relieve` walks.
    def spaces(self) -> dict[str, dict]:
        return {name: self.space(name) for name in self.volumes}

    # Writes `{server, ts, url, usage, usage_at, space, units, mirrors: {server: n copies}}` to
    # `platform/resources/<server>/heartbeat` and returns it. `units` is how the index discovers subsystems;
    # `mirrors` is how `restore` and the index find who holds copies.
    def heartbeat(self) -> dict:
        hb = {"server": self.server, "ts": self.wall(), "url": self.url,
              "schema": SCHEMA, "build": BUILD,                      # what this build understands, and what it is
              "usage": self.usage_cached(), "usage_at": self.usage_at,
              "space": self.space(), "volumes": self.spaces(), "units": self.units(),
              "mirrors": {s: sum(len(mirrored_buckets(r, s, self.bucket_seconds)) for r in self.volumes.values())
                          for r in self.volumes.values() for s in mirrored_servers(r)}}
        self.objects.put(f"{RESOURCES}/{self.server}/heartbeat", json.dumps(hb).encode())
        return hb

    # `resources_seen` filtered to heartbeats younger than `lost_after`.
    def live_resources(self) -> dict[str, dict]:
        now = self.wall()
        return {s: hb for s, hb in resources_seen(self.objects).items() if now - float(hb["ts"]) <= self.lost_after}

    # -- the policy pass ------------------------------------------------------------------
    # For each subsystem and unit, delete bucket files whose `end` is older than `retention_days` — files
    # only; a subsystem that indexes its buckets (the VMS's manifest) drops the lines in its own hook. The
    # resource's own database forgets each removed path. Returns the count. The test sets `other/retention {days: 1}`, advances three days and sees exactly the
    # `other` bucket go.
    def retain(self) -> int:
        """Each subsystem's buckets by its own days. Files only: a subsystem that
        indexes its buckets (the VMS's manifest) drops the lines in its own pass."""
        removed = []
        for sub, units in self.units().items():
            for unit in units:
                days = retention_days(self.vars, sub, unit)
                for path in self.volumes.values():
                    for b in buckets_under(path, sub, unit, self.bucket_seconds):
                        if b.end < self.wall() - days * 86400:
                            os.remove(os.path.join(path, b.path)); removed.append(b.path)
        if removed and self.database is not None:
            self.database.forget(self.server, removed)                  # the rows go with the file
        return len(removed)

    # The knob. If disabled, `{enabled: False, mirrored: 0, peers: []}`. Otherwise, for each peer from
    # `peers_of`, ask what it already holds and `put` every closed bucket it lacks — any subsystem's,
    # exactly once each, by the server that owns it. Returns `{enabled, mirrored, peers}`. The test shows
    # two buckets mirrored the first pass and zero the second.
    def mirror(self) -> dict:
        """The knob. Every CLOSED bucket on this server — any subsystem — is
        copied to the next live resource(s) after it, exactly once each (the
        peer says what it already holds), by the server that owns it."""
        knob = mirror_settings(self.vars)
        if not knob["enabled"]:
            return {"enabled": False, "mirrored": 0, "peers": []}
        live = self.live_resources()
        peers = peers_of(self.server, list(live), knob["copies"])
        n = 0
        for peer in peers:
            have = {b.path for b in self.peers.mirrored(live[peer]["url"], self.server)}
            for b in self.closed_buckets():
                if b.path in have:
                    continue
                with open(self.path_of(b.path), "rb") as f:
                    self.peers.put(live[peer]["url"], self.server, b.path, f.read())
                n += 1
        return {"enabled": True, "mirrored": n, "peers": peers}

    # The reverse, run by the owner: for every live peer whose heartbeat lists me under `mirrors`, pull each
    # of my buckets it holds that I do not have (tmp + rename), then, if anything came back, run every
    # registered hook once so the subsystem re-indexes. Returns `{pulled, <sub>.<key>: …}`. In the test,
    # `srv-a` with a wiped disk pulls 2 buckets; the open bucket that was never mirrored is the RPO.
    def restore(self) -> dict:
        """The reverse, run by the owner: pull my buckets from whoever holds
        copies, then let each subsystem's hook re-index what came back."""
        pulled = 0
        for peer, hb in self.live_resources().items():
            if peer == self.server or self.server not in hb.get("mirrors", {}):
                continue
            for path in sorted(b.path for b in self.peers.mirrored(hb["url"], self.server)):
                dest = self.path_of(path)          # back onto the volume that held it, or the emptiest
                if os.path.exists(dest):
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest + ".tmp", "wb") as f:
                    f.write(self.peers.get(hb["url"], self.server, path))
                os.replace(dest + ".tmp", dest); pulled += 1
        hooks = {sub: h.pass_(self.wall()) for sub, h in self.hooks.items()} if pulled else {}
        return {"pulled": pulled, **{f"{s}.{k}": v for s, r in hooks.items() for k, v in r.items()}}

    # -- the watermark -------------------------------------------------------------------
    # Retention by days is a PROMISE to the operator; this is what happens when the promise cannot be kept.
    # The resource measures — the disk, not the tree — and says how many bytes to free; each subsystem
    # decides what to give up, because only it knows what its files mean. Nothing here knows what a camera
    # is, and nothing here deletes a subsystem's file.
    #
    # Over `high`, free down to `low`. Slowness resolves itself: a hook that can only start something
    # (evacuating footage to the server that now writes it, say) returns what it managed and is asked again
    # on the next pass — which is why there is no third, "critical" mark and no separate schedule.
    def relieve(self) -> dict:
        """Over the high mark, ask each subsystem to free bytes down to the low one.

        Per VOLUME, and that is the whole difference from the single-disk case:
        space does not average. A box that is 50% full across two disks, one of
        them at 98%, is a box that stops recording — and freeing bytes on the
        empty one closes nothing, because the unit that cannot write is on the
        full one. So the loop is over volumes, and the volume goes to the hook:
        only the subsystem knows which of its files are where, but only the
        resource knows which disk is short.
        """
        knob = space_settings(self.vars)
        if not knob["enabled"]:
            return {"space": "off"}
        out, worst, over = {}, 0.0, []
        for name in self.volumes:
            sp = self.space(name)
            worst = max(worst, sp["full"])
            if not sp["total"] or sp["used"] <= sp["total"] * knob["high"]:
                continue
            need, freed = int(sp["used"] - sp["total"] * knob["low"]), 0
            for sub, h in self.hooks.items():
                free = getattr(h, "free", None)
                if free is None:
                    continue                                 # a subsystem that keeps only buckets: `retain` is its whole policy
                rep = _ask_to_free(free, need - freed, self.wall(), knob["min_days"], name)
                freed += int(rep.get("freed", 0))
                out.update({f"{sub}.{k}": v for k, v in rep.items()} if len(self.volumes) == 1
                           else {f"{sub}.{name}.{k}": v for k, v in rep.items()})
                if freed >= need:
                    break
            over.append({"volume": name, "full": round(sp["full"], 3), "need": need, "freed": freed,
                         "short": max(0, need - freed)})
        if not over:
            return {"space": "ok", "full": round(worst, 3)}
        first = over[0]                                      # single-volume callers read these three at the top level
        return {"space": "over", "full": first["full"], "need": first["need"], "freed": first["freed"],
                "short": first["short"], "volumes": over, **out}

    # The timer's body, in order: each subsystem's hook (it may index or drop lines), then `retain`, then
    # `relieve` — the promise first, the watermark only for what the promise left behind — then `mirror`;
    # results flattened into one dict (`<sub>.<key>`, `removed`, `space`, `enabled`, `mirrored`, `peers`).
    def pass_(self) -> dict:
        out = {}
        for sub, h in self.hooks.items():                    # a subsystem's own pass first: it may index or drop lines
            out.update({f"{sub}.{k}": v for k, v in h.pass_(self.wall()).items()})
        out["removed"] = self.retain()
        self.last_usage, self.usage_at = self.usage(), self.wall()    # the one walk of the pass, not one per heartbeat
        out["usage"] = self.last_usage
        out.update(self.relieve())
        out.update(self.mirror())
        return out


# The resource over HTTP, in a daemon thread. `extra(path, headers) -> (status, bytes[, headers]) | None`
# lets a subsystem add its own reads (the VMS: manifests and footage).
#
# #### `class H(BaseHTTPRequestHandler)` (nested)
# - `log_message` — silenced.
# - `_raw(status, body, headers=())` — send a status, `Content-Length`, optional headers and the bytes.
# - `do_GET`:
#   - `GET /buckets/<sub>/<unit>` — `buckets_under` for that unit, one `Bucket.line()` per line, 200.
#   - `GET /mirrored/<server>` — `mirrored_buckets` for that server, same format.
#   - `GET /events?from&to&cam&kind&subsystem&unit&limit` — `resource.database.query(...)` as JSON
#     (`{events, state}`, unfenced: the console fences); 503 if the job runs no database.
#         - `GET /events/<path>` — the raw bytes of one bucket; `path` may begin with `.mirror/<server>/`.
#       404 if it contains `..`, does not end in `.events.jsonl`, or is not a file.
#   - anything else — `extra(path, headers)` if given and it answers; otherwise 404.
# - `do_PUT`:
#         - `PUT /mirror/<server>/<path>` — another resource leaves a copy of one of its closed buckets. 400
#       if `..`, empty server, or not `.events.jsonl`; writes to `.mirror/<server>/<path>` via tmp + rename
#       (a copy appears whole or not at all); 204. Any other PUT is 404.
def serve(resource: Resource, host: str = "0.0.0.0", port: int = 8090, extra=None, extra_put=None) -> ThreadingHTTPServer:
    """The resource over HTTP. `extra(path) -> (status, bytes) | None` lets a
    subsystem add its own reads (the VMS: manifests and footage), and
    `extra_put(path, headers, rfile)` its own writes (the VMS: a segment
    arriving from the resource that is giving it up)."""
    root = resource.root

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _raw(self, status, body, headers=()):
            self.send_response(status); self.send_header("Content-Length", str(len(body)))
            for k, v in headers: self.send_header(k, v)
            self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/buckets/"):
                _, _, sub, unit = self.path.split("/", 3)
                return self._raw(200, "".join(b.line() + "\n" for b in buckets_under(root, sub, unit, resource.bucket_seconds)).encode())
            if self.path.startswith("/mirrored/"):
                return self._raw(200, "".join(b.line() + "\n" for b in mirrored_buckets(root, self.path[len("/mirrored/"):], resource.bucket_seconds)).encode())
            if self.path == "/events" or self.path.startswith("/events?"):
                if resource.database is None:
                    return self._raw(503, b'{"error": "this resource runs no event database"}', [("Content-Type", "application/json")])
                q = {k: v[0] for k, v in urllib.parse.parse_qs(self.path.partition("?")[2]).items()}
                rep = resource.database.query(float(q.get("from", 0)), float(q.get("to", 1e12)),
                                           int(q["cam"]) if q.get("cam") else None, q.get("kind"), q.get("subsystem"), q.get("unit"),
                                           limit=int(q.get("limit", 1000)))
                return self._raw(200, json.dumps(rep).encode(), [("Content-Type", "application/json")])
            if self.path.startswith("/events/"):
                rel = self.path[len("/events/"):]; p = os.path.join(root, rel)
                if ".." in rel or not rel.endswith(".events.jsonl") or not os.path.isfile(p):
                    return self._raw(404, b"")
                with open(p, "rb") as f: return self._raw(200, f.read())
            if extra is not None:
                r = extra(self.path, self.headers)
                if r is not None:
                    return self._raw(*r)
            self._raw(404, b"")

        def do_PUT(self):
            if not self.path.startswith("/mirror/"):
                if extra_put is not None:
                    r = extra_put(self.path, self.headers, self.rfile)
                    if r is not None:
                        return self._raw(*r)
                return self._raw(404, b"")
            rel = self.path[len("/mirror/"):]
            server, _, path = rel.partition("/")
            if ".." in rel or not server or not path.endswith(".events.jsonl"):
                return self._raw(400, b"")
            dest = os.path.join(root, MIRROR_DIR, server, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            n = int(self.headers.get("Content-Length", 0))
            with open(dest + ".tmp", "wb") as f:
                f.write(self.rfile.read(n))
            os.replace(dest + ".tmp", dest)                     # a copy appears whole or not at all
            self._raw(204, b"")

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
