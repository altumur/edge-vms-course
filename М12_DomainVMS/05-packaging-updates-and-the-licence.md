# Lesson 5 — Packaging, Updates, and the Licence

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** one pack for three clusters with per-cluster differences and no forks; an update path that works with the vendor unreachable; and an entitlement cache that degrades on a stated grace period without ever stopping a worker.
**Time:** ~120 minutes.

## Why this lesson exists

The first four lessons built things that run. This one is about how they *arrive* — at three clusters that differ in small, legitimate ways, at appliances that may not have seen the vendor in a month, and under a licence that the vendor issues and the customer has to keep working when the vendor is gone. Every one of those is a place where a reasonable engineer builds a push pipeline or a phone-home check, and every one of them is where an air-gapped or badly connected site stops updating.

It is also where the course reads the licence it built on. The product runs on Nomad, and Nomad's licence has terms that decide whether a VMS may embed it. A module about entitlement that did not check its own would be a poor teacher.

> **What you can verify without hardware.** The entitlement cache — verification against the vendor key, grace, degradation, and the property that recording is never refused — is `tests/test_lesson5_entitlement.py`. The four jobspecs and two policies in `deploy/` are written to the documentation and validated against nothing here. Nomad Pack rendering, hawkBit, and an OS bundle delivered through the domain's own update server are the bench's.

## Prerequisites

- **М9 Lesson 2** — RAUC bundles and signatures; what an OS update is.
- **М9 Lesson 3** — rollback, and the health-check ladder an update must pass.
- **М11 Lessons 3 and 5** — the jobspec, the Variable it reads, the ACL policy that goes with it.
- **М11 Lesson 10** and [`kubernetes-vs-nomad.md`](../М11_ClusterVMS/kubernetes-vs-nomad.md) — why Nomad, and its licence.
- **Lesson 4** — the domain cluster's Variables as the place the domain keeps small, consistent things.

## Learning objectives

1. Template one deployment for several clusters and say what may differ and what must not.
2. Name the GitOps gap honestly: push against pull, and what a flaky link does to each.
3. Explain why the domain runs its own update server and what that buys an air-gapped site.
4. Cache the entitlement, verify it against the product's key, degrade it on a stated period, and prove recording never stops.
5. Read Nomad's licence and apply its competitive test to a VMS.

---

## Step 1 — One pack, three clusters

The four processes this module added — the signer, the console, the live gateway, the agent — are four jobspecs in `deploy/`. Read them side by side and mark what changes per cluster:

| | Same everywhere | Differs per cluster |
|---|---|---|
| `domain-signer` | everything | runs in the **domain cluster only** — `region = "north"` is the stated decision |
| `domain-agent` | everything | `DOMAIN_NOMAD_ADDR` — where the domain is, read through forwarding |
| `console` | everything | `CLUSTERS=` — its own cluster, or all of them in the domain cluster's instance |
| `live-gateway` | everything | which server satisfies its constraints (`meta.gpu`, `meta.public_addr`) |

That is the whole per-cluster surface: a region name, an address, a list, and constraints Nomad resolves. Everything else — the image, the resources, the ACL policy, the environment names — is identical, and the right way to ship "identical with four variables" is a **Nomad Pack**: one template, a `variables.hcl` per cluster, a registry the clusters pull from. A fork per cluster is how the south cluster ends up six months behind north with nobody able to say why.

The rule that decides what may be a variable is М11's: a **constraint**, never a server. `live-gateway`'s per-cluster difference is not "which host" but "a host with a GPU and a public address", and Nomad finds it.

## Step 2 — The honest GitOps gap

Two ways to get a pack onto a cluster, and they are not equivalent:

- **Pull.** A site catches up by itself: something on the cluster polls a registry and applies what is new. A link that was down for a week costs a week's delay and nothing else.
- **Push.** CI renders the pack and runs `nomad job run` against each cluster. Your pipeline must *reach* each cluster, at the moment it runs, with credentials for each.

Nomad Pack driven from CI is push. Fleet, which the course looked at for the OS plane, is pull. For a campus with three server rooms on one network, push is fine and simpler. For a fleet of sites on consumer uplinks, push is a pipeline that fails on Tuesday and nobody notices until the south cluster is three releases behind — which is *worse* than a site that updates late, because it is invisible. The module says so rather than glossing it, and the mitigation is the next step.

## Step 3 — The domain runs its own update server

The OS plane already has the answer in the pull direction, and it is М9's: an appliance polls for a bundle, verifies its signature, installs it into the other slot, and rolls back if the health check fails. What М9 did not say is *what it polls*. If it polls the vendor, an air-gapped site never updates and a site behind a bad link updates when the link feels like it.

So **the domain runs its own update server** — Eclipse hawkBit, pull-based, hosted like every other domain service in the domain cluster. Appliances poll *it*; **the vendor publishes to it** and never reaches an appliance directly. That restores pull on the OS plane end to end, and it is what makes an air-gapped domain updatable at all: somebody carries a bundle to the update server, and the boxes fetch it as if nothing were unusual. The signer's role is unchanged — bundles are signed by the vendor's key from М9, and the domain's server merely stores and serves them.

The licence rides the same path. It is a small signed document; it arrives through the same hawkBit as bundles, and it lands in the domain cluster's Variables, which is where the next step reads it from.

## Step 4 — Entitlement, from the domain's side

The vendor issues the licence (М14 Lesson 3 is the issuing side — what it contains, what it binds to, why there is no revocation). The domain's side is a cache with a policy:

```python
class EntitlementCache:
    def install(self, blob):      # verify against the VENDOR key shipped in the product; must name THIS domain
    def status(self):             # valid | grace | degraded | none
    def may_add_camera(self, n):  # the one thing that degrades
    @staticmethod
    def recording_allowed():      # True. There is no code path that returns False.
```

The test installs a thirty-day licence and moves the clock:

```
+0d:   valid     (True,  'valid: 50 camera(s) left')                                        recording: True
+31d:  grace     (True,  'grace: 50 camera(s) left')                                        recording: True
+61d:  degraded  (False, 'entitlement degraded: recording continues, adding cameras does not')  recording: True
```

Three properties, each a sentence on the datasheet. **The licence server can be unreachable for a month and nothing changes**: the licence is cached, and the grace (`GRACE`, thirty days) starts only when it expires. **What degrades is decided here and stated**: record-but-don't-add-cameras is the usual answer, and the message says exactly that. **Nothing that is already recording stops** — `recording_allowed()` is a constant, on purpose, so that a licence dispute is never a customer's dark building. A licence the vendor did not sign, or one naming another domain, is refused at install and the previous one stays.

This is the same shape as placement and identity: something cached from above, with an expiry, degrading on a stated period. Rights, placement and entitlement are not three mechanisms; they are one rule applied three times.

## Step 5 — Reading the licence you built on

Nomad Community Edition is under the **Business Source License 1.1**. The terms that matter, read from the file rather than remembered:

- **Licensor:** IBM. **Change Date:** four years after each version's release, after which that version is under **MPL 2.0**.
- **Additional Use Grant:** production use is permitted — *unless* the work is offered to third parties as a hosted or **embedded** service that competes with the licensor's paid versions of the software.

The question for a VMS is the competitive test. A VMS embeds Nomad to schedule its own workers; it does not offer Nomad, or a scheduler, to anyone. Its customers buy camera recording, and the orchestrator underneath is an implementation detail they cannot reach. That is the analysis in [`COURSE-PLAN.md`](../COURSE-PLAN.md) and [`kubernetes-vs-nomad.md`](../М11_ClusterVMS/kubernetes-vs-nomad.md); the point of putting it in a lesson is that a student who ships a product should have read the licence of the thing it stands on, and should be able to say in one sentence why the product is on the right side of the line — and what would move it to the wrong side (selling "managed Nomad clusters" to the same customers would).

**Deliverable:** one pack, three clusters, the four variables above and nothing else different; an OS bundle delivered through the domain's own update server with the vendor's endpoint blocked at the firewall; and a written analysis of what degrades when the licence server is unreachable for a month — and what does not — with the two numbers (licence lifetime, grace) on it.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| The south cluster runs an older console than north | Push, and the pipeline failed for south without anyone noticing. Either make it pull (a registry the cluster polls) or alarm on the pipeline, not the symptom. |
| An appliance never updates | It polls the vendor, not the domain's server — or the domain's server has no bundle because nobody published to it. `hawkBit`'s target list says which. |
| `LicenceError: signature does not verify` on a real licence | The product's vendor key changed (a new product release) or the licence was re-signed. The key ships in the product; the licence must match the product's generation. |
| `may_add_camera` false on a valid licence | The count. Fifty licensed, fifty configured. The message says so. |
| A licence dispute stops recording | It cannot. If it did, someone added a code path; find it and delete it. |

## Recap

- One pack, per-cluster variables, no forks. What differs is a region, an address, a list and constraints; everything else is identical by construction.
- Push reaches; pull catches up. Nomad Pack from CI is push, and the module says what that costs on a bad link.
- The domain runs its own pull-based update server; the vendor publishes to it and never touches an appliance. That is what makes air-gap work.
- Entitlement is cached, verified against the product's key, graceful for a stated period, and degrades to *record but do not add*. Recording is never refused.
- Nomad's BUSL is fine for a VMS that embeds it and does not sell it. Read the file.

## Exercises

1. Write the `variables.hcl` for a fourth cluster that is the rented one from Lesson 8. Which of the four variables change, and does anything new appear?
2. Make the pipeline push to three clusters and cut the link to one. Say what tells you, and when.
3. Change `GRACE` to seven days and rewrite the datasheet sentence. Then argue for thirty against a sales team that wants ninety.
4. Add a feature flag to the licence (`features: ["record", "analytics"]`) and decide what degrades when it lapses: recording, analytics, or adding cameras? Defend the order.
5. Find the sentence in Nomad's BUSL that would apply if the product offered "your own VMS cluster, hosted" to other integrators. Say whether that is a product you would build on Nomad.

## Where this is going

Everything the domain ships now arrives without the vendor. One thing still arrives by hand: the credential a server uses on the mTLS channel, typed in Lesson 4 and marked temporary. [**Lesson 6**](06-secure-introduction-a-box-joins-the-domain.md) is where a server earns it instead — the hardest problem in the course, and it belongs here because a box joins a *domain*.
