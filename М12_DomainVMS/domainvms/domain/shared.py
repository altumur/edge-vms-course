"""Lesson 12 — shared settings without a database.

Some settings belong to the system and not to any one camera: the retention a camera gets when nobody set
one, the folder tree the console offers, the scenarios that tie one camera's event to another's action.
With a server room they live in that cluster's Variables. With cameras as members there is no such place
— every member is its own cluster — and the domain has no database, by decision.

The domain already publishes one thing of this shape: the identity set (Lesson 4), as an object named by a
pointer, object first. Shared settings are the same, with two differences that come from where the copy is
READ — on every member, when the domain may be gone:

    signed          the document carries the signer's signature, by the key set every member already holds.
                    A copy is believed by its signature, not by who handed it over — so any copy is as good
                    as the original, and a member that got it from a stale mirror or a wrong door cannot be
                    fooled by it
    ordered         (term, rev): a member never replaces a newer document with an older one, whatever
                    arrives. The term is Lesson 15's — the domain re-hosted — and it outranks the revision
    kept            each member keeps the last verified document in its own durable store. The domain can be
                    gone for a month; the settings are where they are read
    defaults        a shared setting is a DEFAULT, resolved when it is read. It is never copied into rows:
                    that would make the agent a writer of rows, and a change of default a write to five
                    hundred cameras, each of which might be off

    domain/shared            in the domain cluster's Variables: {object, rev, term, sha256} — the pointer, and the CAS
    shared/rev-<n>           in the domain cluster's objects: the signed document
    domain/shared            in each member's Variables: the pointer to what it holds, written by its agent
    domain/shared            in each member's durable objects: the document itself
    domain/shared-refused    in each member's Variables: a document it would not take, and why
"""
from __future__ import annotations

import hashlib
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cluster.variables import Conflict

from .federation import Unreachable
from .tokens import KeySet, TokenIssuer, _b64, _unb64

POINTER, OBJECT, REFUSED = "domain/shared", "domain/shared", "domain/shared-refused"


def _canonical(doc: dict) -> bytes:
    return json.dumps({k: v for k, v in doc.items() if k not in ("kid", "sig")}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":")).encode()


def sign(doc: dict, issuer: TokenIssuer) -> dict:
    return {**doc, "kid": issuer.kid, "sig": _b64(issuer.key.sign(_canonical(doc)))}


class NotTaken(Exception):
    pass


def verify(doc: dict, keys: KeySet | None, now: float) -> dict:
    if keys is None:
        raise NotTaken("no key set yet — nothing to check a signature against")
    if not keys.usable(doc.get("kid", ""), now):
        raise NotTaken(f"signed by key {doc.get('kid')!r}, which this member does not trust")
    try:
        Ed25519PublicKey.from_public_bytes(keys.keys[doc["kid"]]).verify(_unb64(doc["sig"]), _canonical(doc))
    except (InvalidSignature, KeyError, ValueError):
        raise NotTaken("the signature does not verify")
    return doc


def _order(d: dict) -> tuple[int, int]:
    return int(d.get("term", 0)), int(d.get("rev", 0))


class SharedSettings:
    """The domain's side: edit, sign, publish object-first, point by CAS."""

    def __init__(self, domain_vars, domain_objects, issuer: TokenIssuer, wall=time.time, term=lambda: 1):
        self.vars, self.objects, self.issuer, self.wall, self.term = domain_vars, domain_objects, issuer, wall, term

    def current(self) -> tuple[dict, int]:
        ptr, idx = self.vars.get(POINTER)
        if not ptr:
            return {"rev": 0, "term": self.term(), "settings": {}}, idx
        return json.loads(self.objects.get(ptr["object"])), idx

    # `base_rev` is the revision the editor was looking at. Two editors on one document are Lesson 3's two
    # tabs: the second is told, not overwritten — the CAS on the pointer is what tells them.
    def edit(self, mutate, base_rev: int, by: str | None = None) -> int:
        doc, idx = self.current()
        if int(doc["rev"]) != base_rev:
            raise Conflict(f"shared settings are at rev {doc['rev']}; the edit was made against rev {base_rev}")
        settings = json.loads(json.dumps(doc.get("settings", {})))
        mutate(settings)
        new = sign({"rev": int(doc["rev"]) + 1, "term": self.term(), "at": self.wall(), "by": by,
                    "settings": settings}, self.issuer)
        raw = json.dumps(new, ensure_ascii=False, sort_keys=True).encode()
        key = f"shared/rev-{new['rev']}"
        self.objects.put(key, raw)                                                           # 1. the object
        self.vars.put(POINTER, {"object": key, "rev": new["rev"], "term": new["term"],        # 2. the pointer, by CAS
                                "sha256": hashlib.sha256(raw).hexdigest()}, cas=idx)
        return new["rev"]

    # What the console shows beside the settings: which members hold which revision. Read from each
    # member's own copy of the pointer — the agent's report, as Lesson 9's outcomes are.
    def delivery(self, fed) -> dict:
        ptr, _ = self.vars.get(POINTER)
        want = (int(ptr["term"]), int(ptr["rev"])) if ptr else (0, 0)
        out = {"rev": want[1], "holding": [], "behind": {}, "refused": {}, "silent": []}
        for name, c in fed.clusters.items():
            if c.is_domain_cluster:
                continue
            try:
                have, _ = c.vars.get(POINTER)
                refused, _ = c.vars.get(REFUSED)
            except Unreachable:
                out["silent"].append(name)
                continue
            if refused and (int(refused.get("term", 0)), int(refused.get("rev", 0))) == want:
                out["refused"][name] = refused.get("reason", "")
            elif have and (int(have["term"]), int(have["rev"])) >= want:
                out["holding"].append(name)
            else:
                out["behind"][name] = int(have["rev"]) if have else 0
        out["sentence"] = (f"rev {want[1]} on {len(out['holding'])} of {len(out['holding']) + len(out['behind']) + len(out['refused']) + len(out['silent'])} members"
                           + (f"; not answering: {', '.join(sorted(out['silent']))}" if out["silent"] else "")
                           + (f"; refused by: {', '.join(sorted(out['refused']))}" if out["refused"] else ""))
        return out


# The agent's side, one pass: carry the document home if it is newer than what the member holds and it
# verifies against the member's OWN key set. Object first, then the pointer — a member that dies between
# the two still points at a document it has.
#
# The same carry takes Lesson 15's backup of the domain's state home, under other names: `src` is the pointer
# in the domain cluster, `dst` the member's copy of it, `obj` where the member keeps the document.
def carry(domain_vars, domain_objects, member_vars, member_objects, keys: KeySet | None, now: float,
          src: str = POINTER, dst: str = POINTER, obj: str = OBJECT, refused: str = REFUSED) -> str:
    ptr, _ = domain_vars.get(src)
    if not ptr:
        return "nothing published"
    have, hidx = member_vars.get(dst)
    if have and (int(have["term"]), int(have["rev"])) >= (int(ptr["term"]), int(ptr["rev"])):
        return "up to date" if (int(have["term"]), int(have["rev"])) == (int(ptr["term"]), int(ptr["rev"])) else "holding newer"
    raw = domain_objects.get(ptr["object"])
    try:
        if raw is None:
            raise NotTaken(f"the pointer names {ptr['object']} and there is no such object")
        if hashlib.sha256(raw).hexdigest() != ptr["sha256"]:
            raise NotTaken("the object does not match the pointer's checksum")
        doc = verify(json.loads(raw), keys, now)
        if _order(doc) != (int(ptr["term"]), int(ptr["rev"])):
            raise NotTaken(f"the object is term {doc.get('term')} rev {doc.get('rev')}, the pointer says otherwise")
    except NotTaken as e:
        _, ridx = member_vars.get(refused)
        member_vars.put(refused, {"rev": ptr["rev"], "term": ptr["term"], "reason": str(e), "at": now}, cas=ridx)
        return f"refused: {e}"
    member_objects.put(obj, raw)
    member_vars.put(dst, {"rev": ptr["rev"], "term": ptr["term"], "sha256": ptr["sha256"]}, cas=hidx)
    return f"took rev {ptr['rev']}"


class SharedView:
    """What a member's console reads: the document its agent took, checked again on the way in — a copy on
    flash is still only data — and the defaults resolved against a row."""

    def __init__(self, member_vars, member_objects, wall=time.time):
        self.vars, self.objects, self.wall = member_vars, member_objects, wall

    def document(self) -> dict | None:
        raw = self.objects.get(OBJECT)
        if raw is None:
            return None
        from .agent import ClusterTrust
        try:
            return verify(json.loads(raw), ClusterTrust(self.vars).keyset(), self.wall())
        except NotTaken:
            return None

    def settings(self) -> dict:
        doc = self.document()
        return doc["settings"] if doc else {}

    # A field the camera set is the camera's. A field it did not set takes the domain's default, and says
    # so: the console shows WHERE a value came from, because "why does this camera keep 14 days" must have
    # an answer that is not "somebody, somewhere".
    def effective(self, row: dict) -> dict[str, tuple[object, str]]:
        doc = self.document()
        defaults = (doc or {}).get("settings", {}).get("defaults", {})
        out = {}
        for k in set(defaults) | set(row):
            if row.get(k) is not None:
                out[k] = (row[k], "camera")
            elif k in defaults:
                out[k] = (defaults[k], f"domain rev {doc['rev']}")
        return out
