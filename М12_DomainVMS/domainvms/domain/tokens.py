"""Lesson 4 — a token signed by the domain signer; clusters hold the public
key, never a password hash.

    Alice -> signer: short-lived signed token (sub: alice, exp, jti)
    the cluster's console: verify signature (public key, OFFLINE), check expiry, check the
            revocation list it holds, then look up ITS OWN grants for "alice"

The token names the subject and nothing else. Rights are not in it (the
Authorization row): a token that carried rights would be a lookup that
expired with the domain.

Format: base64url(header).base64url(payload).base64url(Ed25519 signature)
— the JWS shape with one algorithm and no library, so a console verifies it
with forty lines and a public key. Keys are a SET (kid -> public key) so
rotation overlaps: a token signed by the previous key verifies until that
key's retirement time passes.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


class TokenError(Exception):
    pass


class Expired(TokenError):
    pass


class Revoked(TokenError):
    pass


class UnknownKey(TokenError):
    pass


class BadSignature(TokenError):
    pass


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass
class KeySet:
    """What every cluster and agent holds: current kid, and every public key
    still trusted with the time after which each is retired (0 = never)."""
    current: str
    keys: dict[str, bytes] = field(default_factory=dict)       # kid -> raw 32-byte public key
    retire_at: dict[str, float] = field(default_factory=dict)  # kid -> wall time; missing = current/never

    def to_items(self) -> dict:
        items = {"current": self.current}
        for kid, pub in self.keys.items():
            items[f"key:{kid}"] = pub.hex()
            if kid in self.retire_at:
                items[f"retire:{kid}"] = str(self.retire_at[kid])
        return items

    @classmethod
    def from_items(cls, items: dict) -> "KeySet":
        ks = cls(current=items["current"])
        for k, v in items.items():
            if k.startswith("key:"):
                ks.keys[k[4:]] = bytes.fromhex(v)
            elif k.startswith("retire:"):
                ks.retire_at[k[7:]] = float(v)
        return ks

    def usable(self, kid: str, now: float) -> bool:
        return kid in self.keys and (kid not in self.retire_at or now < self.retire_at[kid])


class TokenIssuer:
    """Half of the domain signer. Holds the private key; issues short tokens."""

    def __init__(self, domain: str, private_key: Ed25519PrivateKey | None = None, kid: str | None = None):
        self.domain = domain
        self.key = private_key or Ed25519PrivateKey.generate()
        self.kid = kid or secrets.token_hex(4)
        self.previous: list[tuple[str, bytes, float]] = []    # (kid, public, retire_at) still in the set

    @property
    def public_bytes(self) -> bytes:
        from cryptography.hazmat.primitives import serialization
        return self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def keyset(self) -> KeySet:
        ks = KeySet(current=self.kid, keys={self.kid: self.public_bytes})
        for kid, pub, until in self.previous:
            ks.keys[kid] = pub
            ks.retire_at[kid] = until
        return ks

    def issue(self, subject: str, lifetime: float, now: float | None = None, **claims) -> str:
        now = time.time() if now is None else now
        header = {"alg": "EdDSA", "kid": self.kid}
        payload = {"iss": self.domain, "sub": subject, "iat": now, "exp": now + lifetime,
                   "jti": secrets.token_hex(8), **claims}
        signing = _b64(json.dumps(header, sort_keys=True).encode()) + "." + _b64(json.dumps(payload, sort_keys=True).encode())
        return signing + "." + _b64(self.key.sign(signing.encode()))

    def rotate(self, overlap: float, now: float | None = None) -> str:
        """A new signing key; the old public key stays in the set for
        `overlap` seconds so tokens it signed run to their expiry. Returns
        the new kid."""
        now = time.time() if now is None else now
        self.previous.append((self.kid, self.public_bytes, now + overlap))
        self.key, self.kid = Ed25519PrivateKey.generate(), secrets.token_hex(4)
        self.previous = [(k, p, u) for k, p, u in self.previous if u > now]
        return self.kid


def verify(token: str, keys: KeySet, revoked: set[str] = frozenset(), now: float | None = None,
           skew: float = 60.0) -> dict:
    """Offline. Returns the payload (the subject is payload["sub"])."""
    now = time.time() if now is None else now
    try:
        h, p, s = token.split(".")
        header, payload = json.loads(_unb64(h)), json.loads(_unb64(p))
    except (ValueError, json.JSONDecodeError):
        raise BadSignature("not a token")
    kid = header.get("kid", "")
    if not keys.usable(kid, now):
        raise UnknownKey(f"kid {kid!r} is not in the trusted set (or retired)")
    try:
        Ed25519PublicKey.from_public_bytes(keys.keys[kid]).verify(_unb64(s), f"{h}.{p}".encode())
    except InvalidSignature:
        raise BadSignature("signature does not verify")
    if now > payload["exp"] + skew:
        raise Expired(f"expired {now - payload['exp']:.0f}s ago")
    if now < payload["iat"] - skew:
        raise BadSignature(f"issued {payload['iat'] - now:.0f}s in the future — clock skew")
    if payload["jti"] in revoked:
        raise Revoked(payload["jti"])
    return payload


class RevocationList:
    """Small, rare, consistent: raft's shape. Entries carry the token's own
    expiry so the list prunes itself — revocation is a lifetime problem."""

    def __init__(self):
        self.entries: dict[str, float] = {}       # jti -> exp

    def revoke(self, payload: dict) -> None:
        self.entries[payload["jti"]] = float(payload["exp"])

    def prune(self, now: float) -> None:
        self.entries = {j: e for j, e in self.entries.items() if e > now}

    def to_items(self) -> dict:
        return {"jtis": ",".join(f"{j}:{e}" for j, e in sorted(self.entries.items()))}

    @classmethod
    def from_items(cls, items: dict | None) -> "RevocationList":
        rl = cls()
        for part in (items or {}).get("jtis", "").split(","):
            if ":" in part:
                j, e = part.rsplit(":", 1)
                rl.entries[j] = float(e)
        return rl

    @property
    def jtis(self) -> set[str]:
        return set(self.entries)
