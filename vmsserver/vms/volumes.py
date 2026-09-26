"""The archives a recording may be written into: `rec/volumes/<name>`, the
administrator's list, and the claim that makes one of them served."""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # volumes.py — a volume is a PLACE, declared by the operator and served by whoever takes it
#
# **Role in the module.** Until now the archives a box could write into were deployment: a directory per
# disk, an `ARCHIVE` in the unit file, an instance per volume. That works while a volume is a disk somebody
# screwed into a rack. It stops working the day a volume is a bucket: the operator creates it in the
# console, and nobody is going to edit a systemd unit for it.
#
# So a volume becomes a row — `rec/volumes/<name>`, the `tables:` grant of `rec.subsystem.yaml` — and a
# **place** in the sense placement already means (`place_by: volume`). The row is a DECLARATION: it says
# this archive should be written into. It does not say by whom, and it cannot: the row is written on a
# console, and which process serves it is a fact about the cluster at this second.
#
# That fact is a `hold` (`w2cplatform.contract`): `rec/holds/<volume>`, the same row and the same
# CAS-with-a-lease rule as a worker's slot. A recorder with no `VOLUME` pinned takes a free volume it may
# serve and becomes that volume's recorder; it renews while it runs; it lets go on an orderly stop; and if
# it dies the hold lapses and the next free recorder picks the volume up. Nobody assigns, nobody starts a
# process: the controller places recordings on the places that exist, and a place exists because somebody
# is holding it.
#
# **Who may serve what.** A local volume is a disk: only a recorder ON ITS SERVER can write to it. A
# network volume is an address: any box can, and exactly one does, because `servers: distinct` over
# `place_by: volume` means one recorder per place. That asymmetry is the whole reason `server` is a field
# here and empty for the network kind — and it is why one spare per box absorbs one NETWORK volume per
# box, not one per cluster: the spare that takes it may be anywhere.
#
# **Why `quota_bytes` and not free space.** `statvfs` on a bucket answers about the machine, not the
# bucket. A network archive has no free-space number to read, so the operator gives it a ceiling and the
# watermark counts against that. A local volume leaves it at zero and the probe reads the disk, as before.
#
# **What is NOT here.** No mounting, no credentials handling beyond the `*_secret` suffix (the row names
# the key; `secrets.py` keeps it out of every reply), and no uploading: what turns a path into a bucket
# belongs to the archive, not to the list of archives.
#
# ## Public API
# - `Volume` — one row, as a frozen dataclass; `key(name)`, `refuse(fields)`, `write(vars_, fields)`,
#   `delete(vars_, name)`.
# - `declared(vars_)` — every volume row, enabled and not.
# - `servable(vols, server)` — the names a recorder on `server` may take, best first: its own disks, then
#   the network archives anybody may serve.
# - `holders(vars_, sub)` — `{volume: Slot}`: who is serving what, for the console.
# - `served(vars_, sub, now, lost_after)` — the console's view: every declared volume, its holder or None.
# ================================================================================================
from dataclasses import dataclass

from w2cplatform.contract import Slot, Subsystem
from w2cplatform.secrets import is_secret_field
from w2cplatform.spec import Refused

SUB = "rec"
TABLE = "volumes"
KINDS = ("local", "network", "backup")
FIELDS = ("kind", "url", "server", "quota_bytes", "access_secret", "enabled")


# The kinds that are a disk on ONE box, named in `server`. A backup volume is one: a camera's card, or the
# disk of a second server that keeps a copy of critical cameras.
ON_A_BOX = ("local", "backup")


@dataclass(frozen=True)
class Volume:
    """One declared archive. `url` is a directory for a local volume and an
    address for a network one; `quota_bytes` is the ceiling the watermark counts
    against where there is no disk to ask."""
    name: str
    kind: str = "local"
    url: str = ""
    server: str = ""              # local: whose disk. network: empty — any box may serve it
    quota_bytes: int = 0
    access_secret: str = ""       # the `*_secret` suffix: never handed back by a console
    enabled: bool = True

    @classmethod
    def from_items(cls, name: str, items: dict | None) -> "Volume":
        d = items or {}
        return cls(name, str(d.get("kind", "local")), str(d.get("url", "")), str(d.get("server", "")),
                   int(d.get("quota_bytes", 0) or 0), str(d.get("access_secret", "")),
                   str(d.get("enabled", "true")) != "false")

    def to_items(self) -> dict:
        return {"kind": self.kind, "url": self.url, "server": self.server,
                "quota_bytes": self.quota_bytes, "access_secret": self.access_secret,
                "enabled": "true" if self.enabled else "false"}


def key(name: str) -> str:
    return f"{SUB}/{TABLE}/{name}"


# The same door the units have (`SpecController.create`): a name is a name and not a path, because from
# here it becomes a key, an ACL prefix, a directory under the archive root and the `home` of a row. And
# two rules the kinds do not share: a local volume names its server, a network one names a ceiling.
def refuse(fields: dict) -> None:
    name = str(fields.get("name", "") or "")
    if not name:
        raise Refused("a volume needs a name")
    if "/" in name or name in (".", ".."):
        raise Refused(f"a volume name is a name, not a path: {name!r}")
    unknown = [k for k in fields if k not in FIELDS and k != "name"]
    if unknown:
        raise Refused(f"a volume has no field {unknown[0]!r}")
    kind = str(fields.get("kind", "local"))
    if kind not in KINDS:
        raise Refused(f"a volume is {' or '.join(KINDS)}, not {kind!r}")
    if kind == "local" and not str(fields.get("server", "")):
        raise Refused("a local volume is a disk on one server: name it")
    if kind == "backup" and not str(fields.get("server", "")):
        raise Refused("a backup volume is a disk on one box — a camera's card, a second server — and a copy "
                      "is only a copy if you know which box it is on: name it")
    if kind == "network" and str(fields.get("server", "")):
        raise Refused("a network volume is served by whichever box takes it — leave `server` empty")
    # EVERY declared volume has a ceiling, local ones included, and that is the change that lets a disk
    # hold more than one. A volume without a quota means "this whole filesystem", and two of those on one
    # partition both read the same free space and both believe it is theirs — the watermark then frees
    # from one to make room the other immediately takes. A number each is what makes them two volumes and
    # not two names for one. The console fills it with the partition's own size when it declares the first
    # one, so the ordinary answer is a number the operator can then make smaller.
    if int(fields.get("quota_bytes", 0) or 0) <= 0:
        raise Refused("a volume needs `quota_bytes` — how much of the disk is ITS, in bytes "
                      "(the whole partition is a fine answer, and it is what the console offers)")
    url = str(fields.get("url", ""))
    if not url:
        raise Refused("a volume needs a url: the directory it is, or the address it is at")
    # The key never goes in the address, and this is the one place that can still say so. A url is
    # printed on the page, carried in the recorder's heartbeat as `archive`, and written into the row —
    # so `s3://KEY:SECRET@host/bucket` is the same secret in three public places, and the `*_secret`
    # rule cannot help because the field it guards is not the one carrying it. The secret is a VALUE
    # among values (`access_secret`), assembled only by the process that opens the volume.
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        raise Refused("a volume's url names the archive, never the key to it: the credentials go in "
                      "`access_secret` — this string is printed on the page and published in heartbeats")


def write(vars_, fields: dict) -> Volume:
    """Create or replace a declaration. Last write wins on purpose: this is a
    list of archives, not a unit with an epoch — nobody is writing into two
    versions of it at once, and the hold is what makes it exclusive."""
    refuse(fields)
    name = str(fields["name"])
    _, idx = vars_.get(key(name))
    vol = Volume.from_items(name, {k: v for k, v in fields.items() if k != "name"})
    vars_.put(key(name), vol.to_items(), cas=idx)
    return vol


def delete(vars_, name: str) -> None:
    vars_.delete(key(name))


def declared(vars_) -> list[Volume]:
    """Every declared volume, in name order. Includes the disabled ones: the
    console shows them, and `servable` is what filters."""
    out = []
    for path in sorted(vars_.list(f"{SUB}/{TABLE}/")):
        name = path[len(f"{SUB}/{TABLE}/"):]
        items, _ = vars_.get(path)
        if items:
            out.append(Volume.from_items(name, items))
    return out


# What a recorder on this server may take, and in what order. Its own disks first — a local volume has
# exactly one server that can serve it, so leaving it for later risks a spare elsewhere never being able
# to help — then the network archives, which anybody can take and which are therefore the ones a spare is
# for. Disabled volumes are nobody's: the administrator turned them off.
def servable(vols: list[Volume], server: str) -> list[str]:
    mine = [v.name for v in vols if v.enabled and v.kind in ON_A_BOX and v.server == server]
    net = [v.name for v in vols if v.enabled and v.kind == "network"]
    return mine + net


# What to offer an operator who has never declared anything. Every server whose recorder says which
# archive root it writes into, and whose resource says how big that filesystem is, and for which no local
# volume is declared yet: one proposal, named after the server, sized to the partition. A PROPOSAL and not
# a row — nothing here writes configuration on a process's behalf. The operator presses the button, and
# from that moment the disk is a volume with a number on it, which is the whole point: the number can be
# made smaller, and a second volume can have the rest.
def suggest(vars_, objects, sub: Subsystem, now: float, lost_after: float = 45.0) -> list[dict]:
    from w2cplatform.console import heartbeats
    from w2cplatform.resource import resources_seen
    have = {v.server for v in declared(vars_) if v.kind in ON_A_BOX}
    res = resources_seen(objects)
    out = {}
    for _, hb in heartbeats(objects, sub.name + "/").items():
        server, root = str(hb.extra.get("server", "")), str(hb.extra.get("archive", ""))
        if not server or not root or server in have or now - hb.ts > lost_after:
            continue
        total = int(((res.get(server) or {}).get("space") or {}).get("total", 0))
        out[server] = {"name": server, "kind": "local", "url": root, "server": server, "quota_bytes": total,
                       "why": "this box records here and the disk is not declared as a volume"}
    return [out[k] for k in sorted(out)]


def holders(vars_, sub: Subsystem) -> dict[str, Slot]:
    """`{volume: Slot}` from `rec/holds/*` — who took what, lapsed holds included."""
    out = {}
    prefix = f"{sub.name}/holds/"
    for path in sorted(vars_.list(prefix)):
        name = path[len(prefix):]
        items, _ = vars_.get(path)
        out[name] = Slot.from_items(name, items)
    return out


# The console's answer to "what archives are there, and is anybody writing into them". A volume whose
# hold is lapsed or released reads as UNSERVED, with the reason — that is the state an operator has to
# see, because `home: <that volume>` is a preference and would otherwise put the footage somewhere else
# without a word. `wanted`/`serving` is the same arithmetic one line up: how many processes the declared
# list needs, and how many of them exist.
def served(vars_, sub: Subsystem, now: float, lost_after: float = 45.0, objects=None) -> dict:
    vols, held = declared(vars_), holders(vars_, sub)
    broken = _unwritable(objects, sub, now, lost_after) if objects is not None else {}
    rows = []
    for v in vols:
        slot = held.get(v.name)
        live = slot is not None and not slot.released and slot.holder != "" and now <= slot.until
        err = broken.get(v.name) if live else None
        row = {k: x for k, x in v.to_items().items() if not is_secret_field(k)}   # the rule at the source
        rows.append({**row, "name": v.name, "served_by": slot.holder if live and not err else None,
                     "why": None if live and not err else
                            f"held by {slot.holder}, which cannot write there: {err}" if err else
                            "disabled by the administrator" if not v.enabled else
                            "declared, and no recorder has taken it" if slot is None or slot.holder == "" else
                            "the recorder that held it let go" if slot.released else
                            "the recorder that held it went silent"})
    wanted = len([v for v in vols if v.enabled])
    return {"volumes": rows, "wanted": wanted, "serving": len([r for r in rows if r["served_by"]])}


# `{volume: why}` for the volumes a live recorder is holding and cannot write into.
#
# This is the third state, and it exists because the first two hid the worst failure there is. A hold is
# fresh, so the console counted the volume served; no footage was being written; every number on the
# screen was green. A declaration can name a path that is not there, a mount that went away or a bucket
# nobody can reach, and none of that is visible in the row — only the process that opened it knows, so
# the process says so (`volume_error` in its heartbeat) and this reads it.
def _unwritable(objects, sub: Subsystem, now: float, lost_after: float) -> dict[str, str]:
    from w2cplatform.console import heartbeats
    out = {}
    for hb in heartbeats(objects, sub.name + "/").values():
        if now - hb.ts > lost_after:
            continue                                    # a silent recorder's last word is not news about a volume
        vol, err = str(hb.extra.get("volume", "")), str(hb.extra.get("volume_error", ""))
        if vol and err:
            out[vol] = err
    return out



# -- the backup archive (М10B Lesson 26) ---------------------------------------------------------------
# A BACKUP volume holds a second recording of a camera: on the camera's own card, or on a second server's
# disk. The primary recording closes its gaps from it — a link that dropped, the seconds its recorder took
# to move — the way Lesson 16 closes them from a device's archive, except that this archive is OURS: a
# recording, with a manifest, served by a recorder.

def backups(vars_) -> set[str]:
    """The names of the enabled backup volumes."""
    return {v.name for v in declared(vars_) if v.kind == "backup" and v.enabled}


def is_backup(row: dict, vars_=None, names: set[str] | None = None) -> bool:
    """Is this recording a backup copy — homed on a backup volume?"""
    names = backups(vars_) if names is None else names
    return str(row.get("home") or "") in names


# `home` is a preference everywhere else, and for a backup volume that is wrong in both directions. A
# primary recording moved onto the backup volume while its own server rebooted leaves ONE copy where the
# operator paid for two (and on a card, eats the camera's uplink); a backup recording moved off it is not a
# copy at all. So for `rec` it is a filter: a recording homed on a backup volume goes to that volume or
# nowhere, and nothing else goes to a backup volume. `/unplaceable` then says so, which is the honest
# answer to "the card is gone".
def admit_recording(ctl, row: dict, worker: str) -> bool:
    names = backups(ctl.vars)
    if not names:
        return True
    place = ctl.place_of(worker)
    home = str(row.get("home") or "")
    if home in names:
        return place == home
    return place not in names


# A camera with two recordings has its worker placed beside one of them (`near: {sub: rec, of: cam}`).
# Beside the PRIMARY is the obvious choice and the wrong one: when the primary's server falls, it takes the
# worker with it, and the backup loses its stream at exactly the moment it exists for. Beside the backup,
# the worker survives the primary's server, the backup keeps recording, and the primary backfills its move
# from the backup.
def rank_near_recording(ctl, recording_id: str) -> int:
    names = backups(ctl.vars)
    if not names:
        return 0
    items, _ = ctl.vars.get(f"{SUB}/recordings/{recording_id}")
    return 0 if items and str(items.get("home") or "") in names else 1


def _register() -> None:
    from w2cplatform.spec import register_admit, register_near_rank
    register_admit(SUB, admit_recording)
    register_near_rank("vms", rank_near_recording)


_register()
