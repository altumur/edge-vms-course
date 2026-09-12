# Lesson 7 — Lifetimes, Rotation, and Revocation That Works Offline

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a lifetime table chosen from the outage you must survive; renewal that overlaps so nothing drops; a root rotation the domain runs through without stopping; and revocation that needs no list and no network — plus the two pieces of state that make all of it recoverable.
**Time:** ~150 minutes.

## Why this lesson exists

The domain's root is self-signed and it is the top. That was decided in Lesson 4 for a security reason — a vendor-held root above it would be a vendor that can impersonate the customer's whole trust domain — and it has a consequence the module has to face rather than defer: **nobody above will re-issue anything.** If the domain loses its key, every server re-enrolls. If a certificate expires during an outage, the outage becomes a dark building. If a stolen device's certificate is valid for a year, it is valid for a year.

None of that is fixed by choosing carefully. It is fixed by arithmetic — lifetimes derived from the autonomy the product promises — and by drills: a rotation nobody has run is a plan, and a backup nobody has restored from is a hope. Everything in this lesson takes a `now` parameter so the drills run in milliseconds instead of years.

> **What you can verify without hardware.** All of it: `tests/test_lesson7_lifetimes.py` issues certificates with real Ed25519 signatures and moves time — a thirty-day outage, a renewal inside the margin, a root rotation with an overlap window and a cross-certificate, a peer whose clock is an hour behind. The identity restore on another cluster is in Lesson 4's tests. The `openssl`-on-the-wire version of the same chain is М9 Lesson 2's and the bench's.

## Prerequisites

- **Lesson 4** — the signer and the token key set with its overlap.
- **Lesson 6** — the LDevID, which is the one long-lived certificate and the one slow case.
- **М9 Lesson 2** — a chain, a signature, a bundle.
- **М11 Lesson 3** — publish-then-point and the RPO. Users get the same treatment here.

## Learning objectives

1. Split certificates by job and state each one's lifetime, margin, and tolerable outage.
2. Show a thirty-day outage taking the service certificates dark and leaving devices alone.
3. Renew with overlapping validity and say why that is what lets a process reload without dropping a connection.
4. Rotate the root of a live domain: overlap window, cross-certificate, retirement on a date.
5. Argue that revocation is a lifetime problem, not a list problem — and name the one slow case.
6. Recognise clock skew as a named failure and bound it.
7. Back up and restore the two pieces of state the domain cannot regenerate.

---

## Step 1 — The tension, with an arithmetic answer

Short certificates revoke by expiring but a cluster offline longer than the lifetime goes dark. Long ones survive outages and keep a stolen device trusted for months. There is no lifetime good at both, so do not look for one — **split the certificates by job**:

```
root     lifetime    3650 d  margin   365 d  tolerable outage   3285 d
service  lifetime       3 d  margin     1 d  tolerable outage      2 d
ldevid   lifetime     730 d  margin    90 d  tolerable outage    640 d
```

That is `LIFETIMES` in `domain/signer.py`, printed by `max_tolerable_outage()`, and the formula is the sentence to put on the datasheet:

> **maximum tolerable outage = certificate lifetime − renewal margin**

Pick lifetimes from the outage you must survive, not the other way round. A product promising thirty days of autonomy cannot issue three-day service certificates — and the table above says so: `max_tolerable_outage("service")` is two days. That is *correct* for service-to-service certificates, because they never need anything outside the cluster to renew (the signer is in the domain cluster, and a cluster that cannot reach its own signer for two days has bigger problems). It would be wrong for a device, and devices get 640 days.

| Certificate | Lifetime | Renewed by | Needs anything outside the cluster? |
|---|---|---|---|
| The domain root | years | a rotation drill | — |
| Service-to-service | hours to days | the signer | **never** |
| Device identity (LDevID) | long | the signer, on enrollment and renewal | never |

## Step 2 — Thirty days, offline

Issue one of each, then advance the clock a month:

```
after 30 d  service: expired 2332800s ago
after 30 d  ldevid:  ok, issuer acme root g1
```

The read view's service certificate is dark: exactly what the table predicted, and exactly what the console would show as *cluster unreachable* while the other clusters carried on. The box's identity is fine. When the link returns, the service renews against the signer in one round trip and the cluster reappears; the device never noticed. If the outage was instead the *domain cluster* — the signer itself gone — every other cluster's service certificates go dark on day two, and that is the number the datasheet has to say about the domain cluster's own availability.

## Step 3 — Renewal without downtime

`Signer.needs_renewal(cert, kind)` is true once `now` is inside the margin, and `renew()` issues a fresh window for the **same key and the same name**:

```python
c2 = s.renew(c1, "service")
assert bundle.verify(c1, now) and bundle.verify(c2, now)     # both valid: reload without dropping
```

Overlapping validity is the whole trick. A process holding `c1` fetches `c2`, loads it for *new* connections, and lets existing connections run out on `c1` — no listener restart, no dropped stream. The margin is how long you have to do that; one day out of three is generous on purpose, because the renewal is a job that can fail and retry.

## Step 4 — Root rotation as a drill

The root's lifetime is years, and a root that has never been rotated is a root nobody knows how to rotate. So the domain rotates its root *while running*, and the mechanism is an overlap in the **trust bundle** every server and service holds:

```python
new_root, cross = signer.rotate_root(bundle, overlap=7 * DAY)
```

Four things happen in that call. A new root is generated with its own name — `acme root g2`; each generation has its own subject, because a bundle keys on the subject and two roots that share a name are one root to it. The bundle gains the new root and marks the old one **retired at** now + overlap. New leaves are signed by the new root from now on. And a **cross-certificate** is returned: the new root's public key, signed by the old root, for peers that have not yet received the new bundle and only trust `g1`:

```
bundle: {'CN=acme root g1': retires in 7.0 d, 'CN=acme root g2': current}
verify(old_leaf)  -> acme root g1        verify(new_leaf) -> acme root g2
old-only peer, verify(new_leaf, cross=[cross]) -> acme root g2
```

During the window everything verifies everywhere. On the date, `g1` is retired — a leaf it signed fails with *was retired at …* even if its own dates are fine — and by then every leaf that mattered was renewed under `g2` in Step 3's normal course. The signer persists the new root to `domain/signer` with its generation, so a signer rescheduled to another server mid-drill comes up as `g2`. Students rotate a live domain's root because a backup nobody has restored from is a hope and a rotation nobody has run is a plan.

## Step 5 — Revocation is a lifetime problem

CRLs and OCSP both assume you can reach something. The design's revocation is: **let short certificates expire.** A compromised service key is worth exactly the hours until its certificate lapses and the signer declines to renew. A revoked token is on a list the agent carries (Lesson 4) and that list prunes itself as tokens expire. The one slow case is the device certificate — 640 days is a long time to trust a stolen box — and it is compensated by the layer above: the box's *entitlement* and *grants* are short, so a stolen box keeps its identity and loses everything it could do with it. The test issues a device certificate with a three-day lifetime and shows it dark on day four with nobody having done anything: revocation on a schedule stated in advance.

## Step 6 — Clock skew, named

A certificate is valid from *not before* to *not after* according to the verifier's clock. A server whose clock is an hour behind rejects a freshly issued certificate as *not yet valid* and the symptom looks like every other TLS failure. `TrustBundle.verify()` names it:

```
skew: not yet valid: starts in 3540s — clock skew?
```

and tolerates five minutes either side, which is the number NTP keeps a healthy box well inside. Beyond that, the message points at the clock, because the alternative is an engineer re-issuing certificates that were never wrong.

## Step 7 — The two pieces of state, and why they are recoverable

Everything at the domain can be re-provisioned in another cluster from nothing — placement, the read view, the agents — except two things: the **signer's key**, which every certificate in the domain chains to, and, where the customer has no IdP, the **local user records**. Losing the domain cluster loses both, and there is nobody above to re-issue from. So recoverability is **backup, not delegation**:

- `Signer.backup()` is the keys and the root, kept where the domain cluster's death cannot reach — another cluster's object store, or offline. `Signer.restore()` on another cluster brings the same root back, and Lesson 4's test shows old tokens still verifying afterwards.
- The identity set is published object-first with a pointer (Lesson 4), backed up beside the key, and `IdentityStore.restore()` follows the pointer. The RPO for users is the publication interval.

Re-hosting the domain is then М11's restore with different nouns: the backed-up key, the identity object the pointer names, then the domain agents pick up the new public key from the new domain cluster's Variables. And the honest cost, said with a number: **lose the key anyway and every server re-enrolls** — Lesson 6's path, times N, and the module asks how long that takes at your N rather than leaving it as a feeling.

**Deliverable:** simulate a thirty-day cluster outage — service certificates dark, devices fine, everything back on reconnection. Rotate the root under load, with the old-root-only peer served by the cross-certificate and the old root retired on its date. Revoke a device and show it losing access on the schedule stated in advance. Then restore the signer and the users on another cluster from the backup.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Everything expires at once on a Monday | Every certificate was issued in the same minute at install and has the same lifetime. Stagger renewals, or accept that the first renewal cycle is the last synchronised one. |
| A leaf fails right after rotation | The peer has the new bundle but the leaf was issued by `g1` and the overlap was set to zero. Overlap ≥ the longest service lifetime, or the drill is a cutover. |
| *was retired at* on a leaf you renewed | Renewed under the old root because the signer had not persisted `g2` yet — a reschedule mid-rotation. `Signer.__init__` reloads the generation; check `domain/signer`'s `gen`. |
| *no trusted root named …* after a re-host | The restore brought the key but the agents still serve the old bundle. Agents sync from the *new* domain cluster's Variables; point them there. |
| Half the cluster says *not yet valid* | NTP. Always NTP. Then the five-minute tolerance. |

## Recap

- Split certificates by job. Root: years. Service: days, renewed inside the cluster, never needing the outside. Device: long.
- **Tolerable outage = lifetime − margin.** Derive lifetimes from the autonomy promised; put the number on the datasheet.
- Renewal overlaps; the margin is the time to reload without dropping.
- Root rotation is an overlap in the bundle, a cross-certificate for slow peers, and a retirement date. Run it on a live domain.
- Revocation is expiry. The device certificate is the slow case and entitlement compensates.
- Clock skew is a named failure with a tolerance.
- Two pieces of state; backup, not delegation; lose the key and every server re-enrolls.

## Exercises

1. Set the service lifetime to thirty days to "survive the outage". Steal a service key and say how long it is useful, and what you would tell the customer.
2. Rotate the root with an overlap shorter than the LDevID lifetime. Which boxes fall off, and when?
3. Add an OCSP responder to the design. Say which outage it does not survive and why the module did not.
4. Write the runbook for restoring the domain in another cluster: the order of the six steps, and the one you cannot do without a human.
5. The token key set (Lesson 4) and the root (here) rotate independently. Argue for rotating them together on one drill, and then for keeping them separate.

## Where this is going

The domain can now look after its own trust with nothing above it. [**Lesson 8**](08-a-cluster-you-rent-and-a-worker-that-does-not-know-where-it-is.md) changes one thing — where the servers come from — and proves the software cannot tell: a cluster rented from the customer's own cloud account, the bandwidth arithmetic done before the demo, and the same worker deployed three ways with identical artifacts.
