"""Lesson 4 — where users live, and creating a user touches no cluster.

    identity/users/<id>   in the domain cluster's Variables, the signer the only writer
                          local: a scrypt hash; federated: an IdP subject and no secret
    identity/pointer      -> identity/rev-N object: the whole set, published object-first
    users/<id>/prefs      per-user UI configuration as an object; last write wins, with a revision

Nothing about a user ever reaches a worker. Authentication ends in a token
naming the subject; what the subject may do is each cluster's grants,
carried by the domain agent and decided by the cluster's console and gateway.
The RPO for users is the publication interval, and it is stated.

Break-glass is the honest residue: one local account, audited on every
use, alarmed on, rotated after — it reintroduces exactly the password
hash the design removed, and the module says so out loud.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

from cluster.variables import Conflict, Variables

from .signer import Signer

TOKEN_LIFETIME = 15 * 60.0        # the number the product states; Lesson 4 makes students defend it


class AuthError(Exception):
    pass


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex() + ":" + h.hex()


def _check(password: str, stored: str) -> bool:
    salt, h = stored.split(":")
    return secrets.compare_digest(_hash(password, bytes.fromhex(salt)).split(":")[1], h)


@dataclass
class User:
    id: str
    kind: str                      # "local" | "idp"
    roles: list[str]
    pwhash: str = ""               # local only
    idp_subject: str = ""          # idp only
    created: float = 0.0

    def to_items(self) -> dict:
        return {"id": self.id, "kind": self.kind, "roles": ",".join(self.roles), "pwhash": self.pwhash,
                "idp_subject": self.idp_subject, "created": self.created}

    @classmethod
    def from_items(cls, it: dict) -> "User":
        return cls(it["id"], it["kind"], [r for r in it.get("roles", "").split(",") if r], it.get("pwhash", ""),
                   it.get("idp_subject", ""), float(it.get("created", 0)))


class IdentityStore:
    def __init__(self, signer: Signer, vars_: Variables, objects, publish_floor: float = 60.0, now=time.time):
        self.signer, self.vars, self.objects, self.now = signer, vars_, objects, now
        self.floor, self._last_publish, self._dirty = publish_floor, -1e9, False
        self.published_rev, self.publishes = 0, 0

    # -- records ----------------------------------------------------------------
    def _path(self, uid: str) -> str:
        return f"identity/users/{uid}"

    def get(self, uid: str) -> User | None:
        items, _ = self.vars.get(self._path(uid))
        return User.from_items(items) if items else None

    def _put(self, u: User) -> None:
        _, idx = self.vars.get(self._path(u.id))
        self.vars.put(self._path(u.id), u.to_items(), cas=idx)
        self._dirty = True

    def create_local(self, uid: str, password: str, roles: list[str]) -> User:
        if self.get(uid):
            raise ValueError(f"user {uid} exists")
        u = User(uid, "local", roles, pwhash=_hash(password), created=self.now())
        self._put(u)
        return u

    def create_federated(self, uid: str, idp_subject: str, roles: list[str]) -> User:
        """The customer has an IdP: the record holds a subject, not a person, and no secret."""
        u = User(uid, "idp", roles, idp_subject=idp_subject, created=self.now())
        self._put(u)
        return u

    def set_roles(self, uid: str, roles: list[str]) -> None:
        u = self.get(uid)
        if not u:
            raise KeyError(uid)
        u.roles = roles
        self._put(u)

    def delete(self, uid: str) -> None:
        _, idx = self.vars.get(self._path(uid))
        self.vars.put(self._path(uid), {"id": uid, "kind": "deleted"}, cas=idx)
        self._dirty = True

    def users(self) -> list[User]:
        out = []
        for p in self.vars.list("identity/users/"):
            items, _ = self.vars.get(p)
            if items and items.get("kind") in ("local", "idp"):
                out.append(User.from_items(items))
        return out

    # -- authentication: ends in a token naming the subject and nothing else ---
    def login(self, uid: str, password: str) -> str:
        u = self.get(uid)
        if not u or u.kind != "local" or not _check(password, u.pwhash):
            raise AuthError("bad credentials")
        return self.signer.tokens.issue(uid, TOKEN_LIFETIME, now=self.now())

    def login_federated(self, idp_assertion: dict) -> str:
        """The IdP authenticated Alice; the signer issues a DOMAIN token naming
        her, and the clusters never learn the IdP exists."""
        subj = idp_assertion["sub"]
        for u in self.users():
            if u.kind == "idp" and u.idp_subject == subj:
                return self.signer.tokens.issue(u.id, TOKEN_LIFETIME, now=self.now())
        raise AuthError(f"no domain user for IdP subject {subj}")

    # -- publish-then-point: the identity set as one object ------------------------
    def publish(self, force: bool = False) -> bool:
        if not (self._dirty or force):
            return False
        if not force and self.now() - self._last_publish < self.floor:
            return False
        rev = self.published_rev + 1
        blob = json.dumps({"format": 1, "revision": rev, "users": [u.to_items() for u in self.users()]}).encode()
        self.objects.put(f"identity/rev-{rev}", blob)                                     # 1. the object
        _, idx = self.vars.get("identity/pointer")
        self.vars.put("identity/pointer", {"object": f"identity/rev-{rev}", "revision": rev}, cas=idx)   # 2. the pointer
        self.published_rev, self._last_publish, self._dirty, self.publishes = rev, self.now(), False, self.publishes + 1
        return True

    @classmethod
    def restore(cls, signer: Signer, new_vars: Variables, objects, pointer_items: dict, now=time.time) -> "IdentityStore":
        """Re-hosting the domain: the backed-up key (Signer.restore), then the
        identity object the pointer names. М11's restore with different nouns."""
        blob = objects.get(pointer_items["object"])
        if blob is None:
            raise RuntimeError(f"pointer names {pointer_items['object']} but the object store has no such object — refusing to guess")
        d = json.loads(blob)
        st = cls(signer, new_vars, objects, now=now)
        for it in d["users"]:
            st._put(User.from_items(it))
        st.published_rev, st._dirty = int(d["revision"]), False
        _, idx = new_vars.get("identity/pointer")
        new_vars.put("identity/pointer", dict(pointer_items), cas=idx)
        return st

    # -- per-user configuration: an object, last write wins, with a revision --------
    def get_prefs(self, uid: str) -> tuple[dict, int]:
        raw = self.objects.get(f"users/{uid}/prefs")
        if not raw:
            return {}, 0
        d = json.loads(raw)
        return d["prefs"], int(d["revision"])

    def put_prefs(self, uid: str, prefs: dict, base_revision: int) -> int:
        """Returns the new revision; raises Conflict if `base_revision` is
        stale so a second tab is TOLD rather than silently overwritten."""
        _, current = self.get_prefs(uid)
        if base_revision != current:
            raise Conflict(f"prefs revision {current}, you had {base_revision}")
        self.objects.put(f"users/{uid}/prefs", json.dumps({"revision": current + 1, "prefs": prefs}).encode())
        return current + 1


@dataclass
class BreakGlass:
    """One account. Audited on every use, alarmed on, rotated after."""
    signer: Signer
    pwhash: str
    audit: list[dict] = field(default_factory=list)
    alarm: list[str] = field(default_factory=list)
    used_since_rotation: int = 0

    @classmethod
    def create(cls, signer: Signer, password: str) -> "BreakGlass":
        return cls(signer, _hash(password))

    def use(self, password: str, who: str, why: str, now: float) -> str:
        ok = _check(password, self.pwhash)
        self.audit.append({"at": now, "who": who, "why": why, "ok": ok})
        self.alarm.append(f"BREAK-GLASS {'used' if ok else 'ATTEMPTED'} by {who}: {why}")
        if not ok:
            raise AuthError("break-glass: bad password")
        self.used_since_rotation += 1
        return self.signer.tokens.issue("break-glass", TOKEN_LIFETIME, now=now, via="break-glass", who=who)

    def rotate(self, new_password: str) -> None:
        self.pwhash, self.used_since_rotation = _hash(new_password), 0
