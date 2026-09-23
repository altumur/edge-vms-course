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
KINDS = ("local", "network")
FIELDS = ("kind", "url", "server", "quota_bytes", "access_secret", "enabled")


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
    if kind == "network":
        if str(fields.get("server", "")):
            raise Refused("a network volume is served by whichever box takes it — leave `server` empty")
        if int(fields.get("quota_bytes", 0) or 0) <= 0:
            raise Refused("a network volume needs `quota_bytes`: there is no disk to ask how full it is")
    if not str(fields.get("url", "")):
        raise Refused("a volume needs a url: the directory it is, or the address it is at")


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
    mine = [v.name for v in vols if v.enabled and v.kind == "local" and v.server == server]
    net = [v.name for v in vols if v.enabled and v.kind == "network"]
    return mine + net


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
def served(vars_, sub: Subsystem, now: float, lost_after: float = 45.0) -> dict:
    vols, held = declared(vars_), holders(vars_, sub)
    rows = []
    for v in vols:
        slot = held.get(v.name)
        live = slot is not None and not slot.released and slot.holder != "" and now <= slot.until
        row = {k: x for k, x in v.to_items().items() if not is_secret_field(k)}   # the rule at the source
        rows.append({**row, "name": v.name, "served_by": slot.holder if live else None,
                     "why": None if live else
                            "disabled by the administrator" if not v.enabled else
                            "declared, and no recorder has taken it" if slot is None or slot.holder == "" else
                            "the recorder that held it let go" if slot.released else
                            "the recorder that held it went silent"})
    wanted = len([v for v in vols if v.enabled])
    return {"volumes": rows, "wanted": wanted, "serving": len([r for r in rows if r["served_by"]])}
