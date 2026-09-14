# entitlement.py — Lesson 5: the licence cached in the domain cluster's Variables, verified against the product's vendor key, graceful for a stated period; recording never stops

**Role in the module.** Lesson 5, entitlement from the domain's side. The vendor issues it (М14 Lesson 3); the domain CACHES it and degrades on a grace period, exactly like placement and identity. The licence lives in the domain cluster's Variables (`domain/licence`, granted to the signer in `deploy/signer-policy.hcl`), is verified against a key shipped in the product, and what degrades is record-but-don't-add-cameras: nothing that is already recording stops because a licence server is unreachable — "not for a month, not ever". Depends on `cryptography` (Ed25519) and М11's `Variables`. Used by the Lesson 5 test; not wired into a process.

## Module-level names
- `GRACE = 30 * 86400.0` — thirty days past `valid_until`: "the number the datasheet states".

## `class LicenceError(Exception)` — bad signature, or a licence for another domain.

## `class Licence` (dataclass)
- `domain: str` — which domain it was issued to; `cameras: int` — the count licensed; `features: list[str]`; `valid_until: float`; `issued: float`.
### `verify(cls, blob, vendor_public) -> Licence`
The blob is `<json body>.<hex Ed25519 signature>` (the same envelope `enroll.Manufacturer.voucher` uses — hex so the `.` separator never appears inside the signature). Splits on the last `.`, verifies the body under the vendor's public key (`LicenceError` on a bad signature or malformed hex), then parses the body.

## `class EntitlementCache`
### `__init__(self, domain, vars_, vendor_public, now=time.time)` — this domain's name, where the cache lives, the product's key, the clock.
### `install(self, blob) -> Licence`
Verify, refuse a licence naming another domain, store `{"blob": <text>}` at `domain/licence` by CAS. `test_licence_verified_cached_and_graceful` proves a licence signed by another key and one for `other-domain` are both refused.
### `current(self) -> Licence | None` — re-reads and re-verifies the cached blob on every call (so a tampered Variable is a `LicenceError`, not a silently wrong count).
### `status(self) -> str`
`none` (nothing installed), `valid` (`now ≤ valid_until`), `grace` (within `GRACE` after), `degraded` (beyond). "Recording is allowed in all four."
### `may_add_camera(self, current_count) -> (bool, str)`
`none`/`degraded` → `(False, "entitlement <st>: recording continues, adding cameras does not")`; at or over `cameras` → `(False, "licensed for N cameras, M configured")`; else `(True, "<st>: K camera(s) left")`. Grace still allows adding.
### `recording_allowed()` (static) → `True`. The comment: "by construction. There is no code path that returns False." The README calls this the Lesson 5 deliverable in one line.

## Notes
- The test walks the sequence the lesson asks for: none → install → valid (99 of 100 may be added, 100 may not) → a month later `grace` (still adding) → `GRACE` later `degraded` (not adding) — `recording_allowed()` true throughout.
- Nothing here talks to a licence server; "unreachable for a month" is modelled purely as the clock passing `valid_until`.
