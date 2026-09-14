# enroll.py — Lesson 6: secure introduction — a pledge with a factory IDevID, the domain's registrar, a simulated manufacturer CA and MASA, the voucher path and the approval queue, and the LDevID that replaces the hand-typed secret

**Role in the module.** Lesson 6, "a box joins the domain". A device with no secret must obtain one, over a network it does not yet trust, from a service it cannot yet authenticate — and М9 removed the easy answer: the image is byte-identical across units, so nothing device-specific can be inside it. The docstring gives the ladder in BRSKI's vocabulary (RFC 8995): the *pledge* (the box, carrying a factory IDevID — a manufacturer-signed certificate), the *registrar* (the DOMAIN's door; decides yes/no; hands the pledge to the signer), the *MASA* (the manufacturer's authority; issues a VOUCHER naming which registrar to trust), the *LDevID* (the certificate from THIS domain, replacing Lesson 4's hand-provisioned credential). Two paths, both auditable: the voucher path (zero-touch — the only thing the customer ever needs from the vendor is the voucher) and registration with human approval (the shipped fallback: a queue, an audit trail, an expiry on unapproved requests). Never a shared secret in an image. The manufacturer is simulated (a CA for IDevIDs and a MASA key); a TPM "cannot be faked in any way worth teaching" — attestation is read, not run. Depends on `signer.Signer` (issues the LDevID with `kind="ldevid"`), `TrustBundle` (the registrar verifies IDevIDs; the pledge verifies its LDevID), and the helpers `_name`, `_utc`, `DAY`, `YEAR`. Used by the Lesson 6 tests only; the real registrar endpoint needs the bench (mTLS).

## `class EnrollError(Exception)` — every refusal on the ladder, with a sentence.

## `class Manufacturer` — the vendor's side (М14)
### `__init__(self, name="vendor", now=time.time)`
A self-signed 20-year IDevID CA (`CN=<name> IDevID CA, O=<name>`, `ca=True`), a separate MASA signing key, and `sold: {serial: customer domain}` — what the MASA knows about who bought which unit.
### `masa_public` (property) — the raw MASA public key: what a domain's registrar is configured with.
### `provision(self, serial) -> (cert, key)`
At the factory: a 20-year IDevID naming the serial (`CN=<serial>`), signed by the IDevID CA; the key "would live in a TPM on real hardware".
### `sell(self, serial, domain)` — records the sale.
### `voucher(self, serial, registrar_id, nonce) -> bytes`
"The MASA vouches: this serial may trust THIS registrar." Refuses unless the serial was sold to the domain in front of the registrar id (`registrar_id.split("/")[0]`) — `test_a_stranger_and_a_wrong_voucher_are_refused`. The voucher is `<json {serial, registrar, nonce, exp: now + DAY}>.<hex signature>` — hex so the separator never appears inside the signature.

## `class Pledge` (dataclass) — the box
- `serial: str`; `idevid`, `idevid_key` — the factory identity; `temp_credential: str | None` — Lesson 4's hand-provisioned stand-in, to be deleted; `ldevid`, `ldevid_key` — filled by enrollment.
### `hello(self, nonce) -> dict`
Generates the future LDevID key, and returns `{serial, nonce, csr_pub (hex), idevid (PEM), proof}` where `proof` is the IDevID key's signature over the JSON of `{serial, nonce, csr_pub}` — proof of possession of the factory key, bound to this nonce and this new public key. "EST, in one line" happens on the other side.

## `class Pending` (dataclass)
A request in the approval queue: `serial`, the `hello` it came with, `requested_at`, `expires_at`.

## `class Registrar` — the domain's door
### `__init__(self, domain, signer, manufacturer_root, masa_public, approval_ttl=24*3600, now=time.time)`
`id` is `<domain>/registrar` (what a voucher must name); `signer` issues; `vendor` is a `TrustBundle` holding the manufacturer's IDevID CA; `masa_public` may be `None` (no voucher path); `approval_ttl` bounds how long an unapproved request waits; `pending`, `audit`.

### `_check_hello(self, hello) -> x509.Certificate`
Load the IDevID and verify it under the vendor bundle at `now` ("a real IDevID from a manufacturer we trust" — a stranger's CA fails with "no trusted root"); verify `proof` under the IDevID's key over the same JSON (else "the pledge does not hold the IDevID's key"); the IDevID's CN must equal the claimed serial (else "IDevID names a different serial").

### `_check_voucher(self, voucher, serial, nonce)`
No MASA key → "this domain has no MASA key; use registration with approval". Verify the MASA signature; then the voucher must name this serial, this registrar id, this nonce, and not be past `exp` — one sentence for all four. A voucher is for one nonce: `test_zero_touch_with_a_voucher` replays it with a new hello and is refused.

### `_issue(self, hello, how) -> x509.Certificate`
`signer.issue(serial, "ldevid", csr_pub)` — the LDevID names the box, not a server; an audit entry `{at, serial, how, ldevid_serial}`.

### `enroll_with_voucher(self, hello, voucher)` — path 1: check hello, check voucher, issue (`how="voucher"`).
### `request(self, hello) -> str` — path 2: check hello, expire stale requests, queue under the serial with `expires_at = now + approval_ttl`, audit `requested`; returns the serial.
### `approve(self, serial, by)` — expire stale, pop the request (none → "no pending request (expired, or never asked)"), audit `approved` with `by`, then `_issue` with `how="approved by <by>"` — so the audit shows both entries (`["requested", "approved", "approved by carol"]` in the test).
### `reject(self, serial, by)` — drop and audit `rejected` if it was pending.
### `expire_pending(self) -> list[str]` — remove every request past `expires_at`, audit `expired unapproved`, return the serials. `test_registration_with_approval_queue_audit_and_expiry`: a request left 3601 s on a 3600 s TTL expires.

## Functions

### `finish(pledge, ldevid, bundle, now)`
The box's last step: verify the LDevID under the domain's trust bundle (it must chain to the domain root the box was handed), keep it, and set `temp_credential = None` — the hand-provisioned credential is deleted, "and nothing stops, because the LDevID verifies under the domain's root".

## Notes
- The registrar keeps no record of used nonces; the same `(hello, voucher)` pair presented twice within the voucher's day would be accepted twice. The nonce binds a voucher to one hello, not to one use.
- `request()` for a serial already pending overwrites the earlier request.
- The approval path has no MASA and therefore no proof the unit was sold to this customer — only that it is a genuine unit of a trusted manufacturer; the human approving is that proof.
