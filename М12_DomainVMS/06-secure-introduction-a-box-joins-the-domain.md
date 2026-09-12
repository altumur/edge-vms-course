# Lesson 6 — Secure Introduction: A Box Joins the Domain

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a box that enrolls from cold with nobody typing a secret — by a manufacturer's voucher, or by a human's approval — receives a certificate from the domain's own signer, and deletes the hand-provisioned credential without anything stopping.
**Time:** ~150 minutes.

## Why this lesson exists

A device with no secret must obtain one, over a network it does not yet trust, from a service it cannot yet authenticate. That sentence has no clean solution, only trades, and М9 removed the easiest one on purpose: **the appliance image is byte-identical across every unit**, so nothing device-specific can be inside it. The lesson is the ladder of what remains, from the option that is a defect to the option that needs hardware, with the shipped fallback built properly in the middle.

It belongs in this module rather than М9 because a box joins a *domain*. The service that decides whether it may — the registrar — is the domain's door, and the signer that issues its certificate has been running since Lesson 4. Nothing about enrollment makes sense until there is something to enroll into.

> **What you can verify without hardware.** Both enrollment paths run end to end in `tests/test_lesson6_enrollment.py` against a simulated manufacturer — a CA for factory identities and a MASA that signs vouchers — with a real X.509 chain and real signatures. A stranger's box and a wrong-domain voucher are refused; unapproved requests expire; the audit trail is asserted. A TPM cannot be faked in any way worth teaching: Step 3's attestation section is read, not run, and says so.

## Prerequisites

- **Lesson 4** — the signer, the mTLS channel, and the per-server credential marked temporary. This lesson deletes it.
- **М9 Lesson 2** — signatures and chains, from the RAUC bundle.
- **М9 Lesson 1** — the identical image, and why.

## Learning objectives

1. Lay out the four approaches to a first secret and say how each fails.
2. Name BRSKI's parts — pledge, registrar, MASA, voucher, IDevID, LDevID — and say which is the domain's and which is the vendor's.
3. Enroll a box zero-touch with a voucher.
4. Build registration with approval as the shipped fallback: a queue, an audit trail, an expiry.
5. Delete the hand-provisioned credential and show nothing stops.
6. Say precisely what a TPM's attestation proves and does not.

---

## Step 1 — The ladder

| Approach | How it fails |
|---|---|
| **Shared secret in the image** | One extracted image is every device's identity. This is how vendors get breached; not a trade-off, a defect |
| **Per-device token written at manufacture** | Works, but it is a factory process, a secret database, and a secret in transit — the problem moved to logistics |
| **Hardware root** (TPM 2.0, or a manufacturer-installed certificate) | Strongest. A key that cannot be exported, and with attestation, evidence of *what software is running* — at the cost of a hardware requirement |
| **Registration with human approval** | The device presents itself; an administrator approves it in the domain's console. Pragmatic, widely deployed, judgement lives in the approval — but it trusts the network at first contact |

The course's rule: never the first; approval as the honest start; hardware identity where the box has it; BRSKI when the customer wants zero-touch and the vendor runs the service it needs.

## Step 2 — The vocabulary, because it names the parts precisely

RFC 8995 (BRSKI) gives every actor a name, and the names make the ownership obvious:

| | Who | What |
|---|---|---|
| **Pledge** | the box | carries a factory **IDevID** — a certificate signed by the manufacturer, naming the serial |
| **Registrar** | **the domain's** | the door a box knocks on to join *this* domain; decides yes or no; hands the box to the signer |
| **MASA** | **the vendor's** (М14) | the manufacturer's authority; issues a **voucher** telling the pledge which registrar to trust |
| **LDevID** | issued by **the domain's signer** | the certificate from *this* domain — replacing Lesson 4's hand-provisioned credential |

The only part of that which is not the domain's is the MASA, and the voucher is the single cryptographic thing a customer ever needs from the vendor. Everything else — the decision, the certificate, the audit — is the customer's, which is the delegation principle from Lesson 4 with the vendor on the far side of it.

`domain/enroll.py` has all four. `Manufacturer` is the vendor's side, simulated: a CA that provisions IDevIDs at the factory, a MASA key that signs vouchers, and a record of which serial was sold to which domain. `Pledge` is the box. `Registrar` is the domain's door, holding the signer, the manufacturer's root it trusts, and the MASA's public key.

## Step 3 — Zero-touch, with a voucher

The box says hello: its serial, a nonce, the public half of a key it just generated for its LDevID, its IDevID, and a proof that it holds the IDevID's private key (a signature over the rest):

```
hello keys: ['csr_pub', 'idevid', 'nonce', 'proof', 'serial']
```

The registrar checks the IDevID chains to a manufacturer it trusts and that the proof verifies under it — a stranger's box, from a manufacturer the domain never enrolled, is refused at this line with *no trusted root*. Then it asks for the voucher. The MASA, which knows the serial was sold to this domain, signs one naming the serial, **this registrar**, **this nonce**, and an expiry:

```
{"exp": 1757586400.0, "nonce": "n1", "registrar": "acme/registrar", "serial": "SN-0001"}
```

A voucher for another domain's registrar, or a replayed one with an old nonce, fails at `_check_voucher`. With both checks passed, the registrar issues — and this is EST in one line — `signer.issue(serial, "ldevid", csr_pub)`:

```
ldevid: CN=SN-0001,O=customer   issuer: CN=acme root g1,O=customer   temp_credential: None
audit:  [{'serial': 'SN-0001', 'how': 'voucher', 'ldevid_serial': 1, 'at': ...}]
```

The certificate names the **serial** — the physical box, before it has a role. The server certificate (Lesson 4) names the server as the domain knows it; the LDevID names the hardware; a box may be re-imaged, re-clustered or re-purposed over its life and its manufacturer's identity should not have to be reissued for any of those reasons. The workers that run on it are allocations and get their tokens from Nomad's workload identity; nothing about a worker is ever in a certificate.

## Step 4 — Registration with approval, built properly

Most customers' vendors do not run a MASA, and most boxes are not the ones the course drew. So the shipped path is approval, and *built properly* means three things the design record lists:

- **A queue.** `Registrar.request(hello)` verifies the IDevID exactly as before — approval does not mean trusting a box that cannot even prove who made it — and parks the request under its serial.
- **An audit trail.** Every transition is a row: *requested*, *approved by carol*, *rejected by carol*, *expired unapproved*. The LDevID's serial number is on the approval row, so the certificate on the wire can be traced to the person who let it in.
- **An expiry.** `approval_ttl` — a request nobody approves does not sit in the queue for a year waiting for someone to click it by mistake; `expire_pending()` removes it and writes the row.

```
[a["how"] for a in reg.audit] == ["requested", "approved", "approved by carol"]
expire_pending() == ["SN-0004"]           # after approval_ttl
```

What approval trusts that the voucher does not: the network at first contact. A box that presents a valid IDevID and gets approved by a human is the right box *if* the human checked the serial on the label against the serial in the queue. The lesson makes students say that sentence, because it is where the judgement lives and where the process is weak.

## Step 5 — Delete the stand-in, and nothing stops

`finish()` verifies the LDevID under the domain's trust bundle, hands it to the box, and sets `temp_credential = None`. Then the test asserts what the deliverable asks: the box still authenticates. It authenticates *better* — its certificate chains to the domain's root, expires on Lesson 7's schedule, renews against the signer without anyone typing anything, and can be revoked by not being renewed. Lesson 4's per-server credential is one of the course's five hand-provisioned stand-ins ([`COURSE-PLAN.md`](../COURSE-PLAN.md) audits them against what resolves each), and this is the one that enrollment resolves.

## Step 6 — What a TPM proves, and does not

Read this step; do not expect to run it.

A TPM 2.0 gives the box a key that cannot be exported — the IDevID's private half lives there, and `Pledge.hello()`'s proof would be a TPM signature. That is *identity*: this is the box with this chip. **Attestation** goes further: the TPM signs a quote of its PCRs, the measurements the firmware and bootloader extended as they ran, and a verifier who knows what the good image measures to can tell that *this software* is running on *this box*. That is what М9's identical image makes possible — every good box measures the same — and it closes the one hole approval leaves open: a box that is the right hardware running the wrong software.

What it does not prove: anything after boot that was not measured; that the software is *correct* rather than *expected*; or that the person who approved it read the label. Attestation moves judgement from a human to a measurement; it does not remove it.

**Deliverable:** a box enrolls from cold with nobody typing a secret, receives an LDevID from the domain signer, and the enrollment is auditable afterwards. Then the hand-provisioned per-server credential is deleted, and nothing stops. Both paths — voucher and approval — and a stranger refused by each.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| *no trusted root* for a box you bought | The registrar does not hold that manufacturer's IDevID CA, or the box's IDevID is from a different product generation. The manufacturer's root is enrolled into the registrar once, on purpose. |
| *the pledge does not hold the IDevID's key* | The hello was replayed or relayed — the proof is over the nonce, and the nonce is per attempt. Possibly an attack; certainly not the box. |
| *voucher does not name this registrar* | The MASA was told a different domain, or the registrar id changed after a re-host. The registrar's id is `<domain>/registrar`, and `domain` does not change on re-host. |
| A request expired before anyone saw it | `approval_ttl` is shorter than the time it takes a human to notice. The console should show the queue on its front page; the number is yours. |
| The LDevID verifies but the mTLS channel refuses it | The server's service certificate (Lesson 4) and the box's LDevID are different certificates for different things. Check which one the channel asked for. |

## Recap

- Never a shared secret in an image. Approval is the honest start; hardware identity where the box has it; BRSKI when the vendor runs a MASA.
- Pledge, registrar, MASA, voucher, IDevID, LDevID. The registrar and the LDevID are the domain's; only the MASA and the voucher are the vendor's.
- The voucher names the serial, the registrar and the nonce; the IDevID proves the box is who the manufacturer says; the registrar issues in one line.
- Approval done properly is a queue, an audit trail and an expiry — and a human who reads the label.
- The last hand-provisioned credential is deleted and nothing stops.
- A TPM proves identity, and with attestation, which software booted. It does not prove the software is correct.

## Exercises

1. Make the registrar accept a hello without the proof and describe the attack that becomes possible with a copied IDevID certificate (not key).
2. Give the voucher no nonce. Replay one. Say what the MASA would have to do instead to stop it.
3. Write the console screen for the approval queue: which fields, in which order, and where the serial on the label is compared to the serial in the row.
4. A box is stolen after enrollment. Lesson 7 revokes its LDevID by not renewing it; say how long it keeps access and where that number was chosen.
5. Sketch what `hello()` looks like when the private key is in a TPM, and what the registrar can additionally ask for.

## Where this is going

The domain now issues every certificate in the estate and its root is the top — self-signed, with nobody above to re-issue anything. That removes a layer and adds a duty. [**Lesson 7**](07-lifetimes-rotation-and-revocation-that-works-offline.md) is where the domain learns to look after its own trust: lifetimes chosen from the outage you must survive, renewal without downtime, root rotation as a drill, and revocation that works when nothing can be reached.
