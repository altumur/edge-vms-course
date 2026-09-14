# signer.py — Lessons 4 and 7: the domain signer — one job, two keys (the CA root and the token issuer) in the Variable `domain/signer`; certificate lifetimes as stated numbers; renewal with overlap; root rotation with a trust bundle and a cross-certificate; clock skew named

**Role in the module.** Lesson 4 (the signer as a service) and Lesson 7 (lifetimes). The CA and the token issuer are the same operational thing — a process that holds keys and signs — so they are one service (`deploy/signer.nomad.hcl`, `signer_service.py`). Its keys live in a Nomad Variable in the domain cluster's raft, `domain/signer`: a SOFTWARE key on purpose, because a TPM-sealed key pins the signer to one server and defeats the failover it just gained; acceptable because everything it signs is short-lived. Lesson 7's split by job: the domain root (years; a rotation drill; needs nothing outside), service-to-service (hours–days; the signer; never a human), device identity / LDevID (long; the signer, on enrollment and renewal) — and the number to state: **maximum tolerable outage = certificate lifetime − renewal margin**. Chain verification takes `now` and a skew tolerance so the tests move time instead of waiting and so clock skew is a named failure. Root rotation is an overlap window in a TRUST BUNDLE (old and new root both trusted until a stated retirement time) with an optional cross-certificate for peers that have only the old root. Depends on `cryptography` (X.509, Ed25519) and `tokens.TokenIssuer`. Used by `identity.IdentityStore` (issues tokens), `enroll.Registrar` (issues LDevIDs; imports `_name`, `_utc`, `DAY`, `YEAR`, `TrustBundle`, `VerifyError`), `signer_service`, and the Lesson 4/6/7 tests.

## Module-level names
- `HOUR`, `DAY`, `YEAR` — seconds.
- `LIFETIMES` — Lesson 7's table as numbers the product states ("change them here, not in a job file"): `root` 10 years with a 1-year margin; `service` 3 days with a 1-day margin (tolerable outage 2 days); `ldevid` 2 years with a 90-day margin (~21 months).

## Functions (helpers)
### `max_tolerable_outage(kind) -> float` — `lifetime − margin`. The docstring: a product promising thirty days of autonomy cannot issue seven-day service certificates. `test_the_number_to_state` checks `service` < 30 days < `ldevid`.
### `_utc(ts)` — a tz-aware datetime for the X.509 builder.
### `_name(cn, org)` — an X.509 Name of `O=<org>, CN=<cn>`.
### `_pem(cert)` — PEM bytes.
### `_key_bytes(k)` — the raw 32-byte private key (what goes into the Variable, hex).

## `class Root` (dataclass)
- `cert: x509.Certificate`, `key: Ed25519PrivateKey`, `retire_at: float = 0.0` (0 = current; the field is not set anywhere — retirement is tracked in the `TrustBundle`).
### `pem` (property) — the certificate as PEM.

## `class VerifyError(Exception)` — every chain failure, with a sentence saying which.

## `class TrustBundle`
"What every server and service holds: the roots it accepts, each with a retirement time. Rotation = add the new root, retire the old on a date." Keyed by the root's subject name (RFC 4514 string), which is why each root generation gets its own CN.
### `__init__(self, roots=None)` — adds each with `retire_at = 0`.
### `add(self, root, retire_at=0.0)` / `retire(self, root, at)` — set the entry; `retire` overwrites the time.
### `pems(self) -> bytes` — every root as PEM, concatenated: the bundle file a server would install.
### `verify(self, cert, now=None, skew=300.0, cross=()) -> str`
Returns the issuing root's CN. Candidates are the bundle's roots plus any `cross` certificate whose *subject* equals the leaf's issuer (never retired). For the first candidate whose subject matches the leaf's issuer: retired at or before `now` → `VerifyError("issuer … was retired at …")`; the leaf's signature must verify under that candidate's public key; `now < notBefore − skew` → "not yet valid … — clock skew?"; `now > notAfter + skew` → "expired …s ago"; else the candidate's CN. No candidate → "no trusted root named …". The 5-minute skew is what `test_revocation_is_a_lifetime_problem_and_clock_skew_is_named` exercises: a peer an hour behind is refused with "clock skew", 200 s behind is fine.

## `class Signer`
"The domain signer. `vars_` is the domain cluster's Variables; the keys are loaded from `domain/signer` or created on first start (the cold start Lesson 1 walks: Nomad up → signer scheduled → certificates issued → workers heartbeat)."

### `__init__(self, domain, vars_, org="customer", now=time.time)`
Reads `domain/signer`. If it exists: rebuild the root from `ca_key`/`ca_cert`, the `TokenIssuer` from `token_key`/`kid`, and `generation` from `gen` (default 1) — a restart anywhere in the domain cluster is the same signer (`test_root_rotation…` restarts as `s2` and finds root g2). If not: generation 1, a new root named `root_cn()`, a fresh token issuer, `_persist()`. `serial` (the leaf serial counter) starts at 0 on every start.

### `root_cn(self) -> str` — `<domain> root g<generation>`. The comment: a trust bundle keys on the subject, and two roots sharing one name would be one root to it.
### `_persist(self)` — `domain/signer` ← `{ca_key, ca_cert, token_key, kid, gen}` by CAS. The signer is the only writer (`signer-policy.hcl`).
### `_new_root(self, cn) -> Root` — a self-signed Ed25519 CA certificate (`BasicConstraints ca=True, path_length=0`, critical), valid from `now − 60` for `LIFETIMES["root"]["lifetime"]`, random serial.

### `issue(self, cn, kind, public_key, lifetime=None) -> x509.Certificate`
A leaf for a SERVER or a service — the docstring: never a worker, because a worker is an allocation named by a slot and Nomad's workload identity is its token. Lifetime from `LIFETIMES[kind]` unless given (the revoke-by-not-renewing test issues a 3-day LDevID); subject `O=org, CN=cn`, issuer the root's subject, `notBefore = now − 60`, incrementing serial, `ca=False`, signed by the root key.

### `needs_renewal(self, cert, kind) -> bool` — `now ≥ notAfter − margin`.
### `renew(self, cert, kind) -> x509.Certificate`
Same key, same CN, a fresh window: "overlapping validity is what lets the holder reload without dropping a connection". `test_renewal_overlaps_so_nothing_drops`: both the old and the new certificate verify at the same instant, same public key, same subject.

### `rotate_root(self, bundle, overlap) -> (Root, x509.Certificate)`
Lesson 7's drill: bump the generation, make a new root, build a cross-certificate (subject = the new root's name, issuer = the old root, the new root's public key, CA, valid for `overlap` seconds, signed by the *old* key), add the new root to the bundle, retire the old root at `now + overlap`, switch `self.root`, persist. Returns the new root and the cross-cert. `test_root_rotation_is_a_drill…`: an old LDevID verifies under g1 and a new one under g2; a bundle holding only g1 verifies the new leaf via `cross=[cross]`; after the overlap the old leaf fails with "retired".

### `backup(self) -> bytes`
The JSON of exactly what `_persist` writes — what must go beyond the domain cluster (another cluster's object store, or offline): "losing this loses the domain's trust: every server re-enrolls".

### `restore(cls, domain, vars_, backup, now=time.time) -> Signer`
Writes the backup into a new cluster's `domain/signer` by CAS and constructs a `Signer` over it — the same root and the same token key, so tokens issued before the move still verify (`test_identity_publishes_object_first_then_pointer_and_restores_elsewhere`).

## Notes
- `TrustBundle.verify` with `cross` uses the cross-certificate's public key to check the leaf but never checks the cross-certificate's *own* signature against a root in the bundle. A peer with only the old root would accept any CA-shaped certificate whose subject matches the leaf's issuer, whoever signed it. See the report.
- `serial` restarts at 0 on every `Signer` construction, so leaf serial numbers repeat across restarts under one root; `Registrar` records `ldevid_serial` in its audit from this counter.
- Only the current root's key is persisted; after rotation the old root's private key is gone (correct — nothing should sign with it), and the old root's certificate survives only in bundles that hold it.
