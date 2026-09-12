"""Lesson 4 and Lesson 7 — the domain signer: one job, two keys.

The CA and the token issuer are the same operational thing — a process that
holds keys and signs — so they are one service. Its keys live in a Nomad
Variable in the domain cluster's raft (domain/signer): a SOFTWARE key on
purpose, because a TPM-sealed key pins the signer to one server and defeats
the failover it just gained. A software key is acceptable because
everything it signs is short-lived.

Certificates, split by job (Lesson 7):

    the domain root          years         a rotation drill         needs nothing outside
    service-to-service       hours–days    the signer               never
    device identity (LDevID) long          the signer, on enrollment and renewal

    maximum tolerable outage = certificate lifetime − renewal margin

Chain verification takes `now` and a skew tolerance, so the tests move time
instead of waiting, and so clock skew is a named failure rather than
"everything broke". Root rotation is an overlap window in a TRUST BUNDLE
(old root and new root both trusted until a stated retirement time), with
an optional cross-certificate for peers that have only the old root.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from .tokens import TokenIssuer

HOUR, DAY, YEAR = 3600.0, 86400.0, 365 * 86400.0

# Lesson 7's table, as numbers the product states. Change them here, not in a job file.
LIFETIMES = {
    "root": {"lifetime": 10 * YEAR, "margin": YEAR},
    "service": {"lifetime": 3 * DAY, "margin": DAY},         # tolerable outage: 2 days
    "ldevid": {"lifetime": 2 * YEAR, "margin": 90 * DAY},     # tolerable outage: ~21 months
}


def max_tolerable_outage(kind: str) -> float:
    """The number to state: lifetime − renewal margin. A product promising
    thirty days of autonomy cannot issue seven-day service certificates."""
    return LIFETIMES[kind]["lifetime"] - LIFETIMES[kind]["margin"]


def _utc(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)


def _name(cn: str, org: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, org), x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_bytes(k: Ed25519PrivateKey) -> bytes:
    return k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


@dataclass
class Root:
    cert: x509.Certificate
    key: Ed25519PrivateKey
    retire_at: float = 0.0           # 0 = current

    @property
    def pem(self) -> bytes:
        return _pem(self.cert)


class VerifyError(Exception):
    pass


class TrustBundle:
    """What every server and service holds: the roots it accepts, each with
    a retirement time. Rotation = add the new root, retire the old on a date."""

    def __init__(self, roots: list[x509.Certificate] | None = None):
        self.roots: dict[str, tuple[x509.Certificate, float]] = {}
        for r in roots or []:
            self.add(r)

    def add(self, root: x509.Certificate, retire_at: float = 0.0) -> None:
        self.roots[root.subject.rfc4514_string()] = (root, retire_at)

    def retire(self, root: x509.Certificate, at: float) -> None:
        self.roots[root.subject.rfc4514_string()] = (root, at)

    def pems(self) -> bytes:
        return b"".join(_pem(r) for r, _ in self.roots.values())

    def verify(self, cert: x509.Certificate, now: float | None = None, skew: float = 300.0,
               cross: list[x509.Certificate] = ()) -> str:
        """Returns the issuing root's CN. `cross` are cross-certificates:
        a new root's public key signed by an old root, for peers that have
        not yet received the new root."""
        now = time.time() if now is None else now
        issuer = cert.issuer.rfc4514_string()
        chain = list(self.roots.values()) + [(c, 0.0) for c in cross if c.subject.rfc4514_string() == issuer]
        for root, retire_at in chain:
            if root.subject.rfc4514_string() != issuer:
                continue
            if retire_at and now >= retire_at:
                raise VerifyError(f"issuer {issuer} was retired at {retire_at:.0f}")
            try:
                root.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
            except InvalidSignature:
                raise VerifyError("signature does not verify under the root it names")
            nvb, nva = cert.not_valid_before_utc.timestamp(), cert.not_valid_after_utc.timestamp()
            if now < nvb - skew:
                raise VerifyError(f"not yet valid: starts in {nvb - now:.0f}s — clock skew?")
            if now > nva + skew:
                raise VerifyError(f"expired {now - nva:.0f}s ago")
            return root.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        raise VerifyError(f"no trusted root named {issuer}")


class Signer:
    """The domain signer. `vars_` is the domain cluster's Variables; the
    keys are loaded from domain/signer or created on first start (the cold
    start Lesson 1 walks: Nomad up → signer scheduled → certificates issued
    → workers heartbeat)."""

    def __init__(self, domain: str, vars_, org: str = "customer", now=time.time):
        self.domain, self.org, self.now = domain, org, now
        items, _ = vars_.get("domain/signer")
        self.vars = vars_
        if items:
            key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(items["ca_key"]))
            self.root = Root(x509.load_pem_x509_certificate(items["ca_cert"].encode()), key)
            self.tokens = TokenIssuer(domain, Ed25519PrivateKey.from_private_bytes(bytes.fromhex(items["token_key"])), items["kid"])
            self.generation = int(items.get("gen", "1"))
        else:
            self.generation = 1
            self.root = self._new_root(self.root_cn())
            self.tokens = TokenIssuer(domain)
            self._persist()
        self.serial = 0

    def root_cn(self) -> str:
        # Each root generation has its own name: a trust bundle keys on the
        # subject, and two roots that share one name are one root to it.
        return f"{self.domain} root g{self.generation}"

    def _persist(self) -> None:
        _, idx = self.vars.get("domain/signer")
        self.vars.put("domain/signer", {"ca_key": _key_bytes(self.root.key).hex(), "ca_cert": self.root.pem.decode(),
                                        "token_key": _key_bytes(self.tokens.key).hex(), "kid": self.tokens.kid,
                                        "gen": self.generation}, cas=idx)

    def _new_root(self, cn: str) -> Root:
        key = Ed25519PrivateKey.generate()
        now = self.now()
        cert = (x509.CertificateBuilder().subject_name(_name(cn, self.org)).issuer_name(_name(cn, self.org))
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(_utc(now - 60)).not_valid_after(_utc(now + LIFETIMES["root"]["lifetime"]))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .sign(key, None))
        return Root(cert, key)

    def issue(self, cn: str, kind: str, public_key: Ed25519PublicKey, lifetime: float | None = None) -> x509.Certificate:
        """A leaf. `cn` names the SERVER or the service, never a worker — a worker
        is an allocation named by a slot, and Nomad's workload identity is its token."""
        now = self.now()
        life = lifetime if lifetime is not None else LIFETIMES[kind]["lifetime"]
        self.serial += 1
        return (x509.CertificateBuilder().subject_name(_name(cn, self.org)).issuer_name(self.root.cert.subject)
                .public_key(public_key).serial_number(self.serial)
                .not_valid_before(_utc(now - 60)).not_valid_after(_utc(now + life))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(self.root.key, None))

    def needs_renewal(self, cert: x509.Certificate, kind: str) -> bool:
        return self.now() >= cert.not_valid_after_utc.timestamp() - LIFETIMES[kind]["margin"]

    def renew(self, cert: x509.Certificate, kind: str) -> x509.Certificate:
        """Same key, same name, a fresh window. Overlapping validity is what
        lets the holder reload without dropping a connection."""
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        return self.issue(cn, kind, cert.public_key())

    # -- Lesson 7: root rotation as a drill --------------------------------------
    def rotate_root(self, bundle: TrustBundle, overlap: float) -> tuple[Root, x509.Certificate]:
        """A new root; the old one stays trusted for `overlap` seconds; new
        leaves are signed by the new root from now. Returns the new root and
        a cross-certificate (new root's key, signed by the old root) for peers
        that only hold the old one."""
        old = self.root
        self.generation += 1
        new = self._new_root(self.root_cn())
        cross = (x509.CertificateBuilder().subject_name(new.cert.subject).issuer_name(old.cert.subject)
                 .public_key(new.key.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(_utc(self.now() - 60)).not_valid_after(_utc(self.now() + overlap))
                 .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                 .sign(old.key, None))
        bundle.add(new.cert)
        bundle.retire(old.cert, self.now() + overlap)
        self.root = new
        self._persist()
        return new, cross

    def backup(self) -> bytes:
        """What goes beyond the domain cluster (another cluster's object
        store, or offline). Losing this loses the domain's trust: every server
        re-enrolls."""
        import json
        return json.dumps({"ca_key": _key_bytes(self.root.key).hex(), "ca_cert": self.root.pem.decode(),
                           "token_key": _key_bytes(self.tokens.key).hex(), "kid": self.tokens.kid,
                           "gen": self.generation}).encode()

    @classmethod
    def restore(cls, domain: str, vars_, backup: bytes, now=time.time) -> "Signer":
        import json
        d = json.loads(backup)
        _, idx = vars_.get("domain/signer")
        vars_.put("domain/signer", d, cas=idx)
        return cls(domain, vars_, now=now)
