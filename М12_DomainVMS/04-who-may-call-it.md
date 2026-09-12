# Lesson 4 — Who May Call It

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a domain signer that issues tokens naming a subject and nothing else; clusters that verify them offline against a public key and check their own grants; an agent that carries trust and grants into every cluster; identity that never reaches a worker; a revocation window stated in advance and then measured; and break-glass, out loud.
**Time:** ~180 minutes.

## Why this lesson exists

Lesson 3 left a write API on every cluster's console, and it is unauthenticated. That is N endpoints where there used to be one. Cluster-owned configuration — one controller per cluster, the only writer of its rows — is why an operator can edit a camera while the domain is unreachable, and it is also why the thing to protect is now per cluster. This is a real cost of the design and it belongs next to the benefit rather than three modules later.

The lesson is in two halves that turn out to be one argument. The first is *what* protects a cluster's API: a channel, then a caller, then a local check that needs no network. The second is a defect the course has been carrying since М9 Lesson 9 — a login against a local `operators` table with a password hash — and what it becomes on N clusters: N Alices, N stealable hashes, and an account that outlives the grants it was meant to bound. Both halves resolve the same way, and it is the move this course keeps making: **delegate an authority; do not distribute a secret.**

> **What you can verify without hardware.** Tokens, the key set, the revocation list, users, grants, the agent, break-glass and the identity restore all run in `tests/test_lesson4_identity_grants_agent.py`, with a clock. The revocation window is stated by `access_ends()` and then measured by moving that clock. The mTLS channel itself — certificates on the wire — is Lesson 7's `TrustBundle` and the bench.

## Prerequisites

- **Lesson 3** — the console's write API and the `verifier` hook it left empty.
- **М11 Lesson 2** — Variables and their ACL: one writer per prefix. The agent is that pattern with a new prefix.
- **М11 Lesson 2** — one writer per prefix, enforced by a policy bound to a job's identity. The agent is one more such writer.
- **М11 Lesson 5** — the cluster's console and its controller: the write path a forwarded edit takes.
- **М9 Lesson 9** — the login marked temporary, and the `operators` and `grants` tables it left behind.

## Learning objectives

1. Order the protections: channel, caller, local check — and say which survive the domain being down.
2. Issue a token that names a subject and nothing else, and verify it offline against a key set.
3. Put users where they belong and prove nothing about them reaches a worker.
4. Carry trust into every cluster with an agent that can write `domain/*` and nothing else.
5. Enforce with cluster-local grants that expire, carried by the agent, and state then measure the revocation window.
6. Say why М9's `operators` table is superseded, not extended — and what break-glass costs.

---

## Step 1 — The channel, before the caller

Every stream in this module — configuration upward, grants downward, status both ways — runs **mTLS from the domain's own self-signed root**. A credential says who is calling; it says nothing about the channel. The root is hand-provisioned in the sense that a student runs `openssl` (or, here, `Signer.__init__`) to make it, and **it is not a stand-in**: this is the customer's root, permanently, and Lesson 7 gives it lifetimes and rotation. The certificate names the **server** — the physical box, which under М10's shape is the thing that has an identity and a place: a worker is an allocation that moves and is named by a slot, and Nomad's workload identity gives its tasks their tokens; what the domain enrols and signs for is the server.

The per-server credential that authenticates on that channel is hand-provisioned in *this* lesson and marked temporary, exactly as М8 hand-provisions AWS keys and М9 a database password. Lesson 6 replaces it with a certificate the server earns by enrolling.

## Step 2 — Delegate an authority, do not distribute a secret

The defect first. М9 Lesson 9 put a login on the console against a local `operators` table. On one box that was right. On N clusters it means four accounts for one person, four passwords she will make identical, four hashes an attacker can take — and worse, **a grant expires and the account does not.** Revoke Alice's grants and her credential still authenticates at every console; you have bounded the authorization window and left the authentication window unbounded.

The fix is the same one the CA made a paragraph ago:

```
Alice ──▶ the domain signer ──▶ a short-lived signed token (sub: alice)
                                        │
                                        ▼
                    south's console: verify signature (public key, OFFLINE)
                                  check expiry, check the revocation list it holds
                                  look up ITS OWN grants for "alice"
```

`domain/tokens.py` is that token: the JWS shape — `base64url(header).base64url(payload).base64url(signature)` — with one algorithm (Ed25519) and no library, so a console verifies it in forty lines and a public key:

```
token:   eyJhbGciOiAiRWREU0EiLCAia2lkIjogImI4MmRi...   (269 bytes)
payload: {'exp': 1757500900.0, 'iat': 1757500000.0, 'iss': 'acme', 'jti': '28ec559f5a84fc9e', 'sub': 'alice'}
```

Read what is not in the payload: no roles, no grants, no cameras. **The token names the subject and nothing else.** What Alice may do is each cluster's own grants (Step 5), because a token that carried rights would be a lookup that expired with the domain. `verify()` returns the payload or raises `Expired`, `Revoked`, `UnknownKey`, `BadSignature` — and takes a `KeySet`, not a key, so that rotation (Lesson 7) is an overlap and not an outage.

**N clusters holding password hashes is N places to steal them from. N clusters holding a public key is zero.** That is a security improvement, not a tidiness one. М9's `operators` table is superseded, not extended: a student who keeps it and adds a `cluster` column has built the N-Alices problem on purpose.

## Step 3 — Where users live, and what touches a cluster

Creating a user touches no cluster. `IdentityStore` writes one record under `identity/users/<id>` in the domain cluster's Variables — small, rare, consistent, beside the signer's key, under the same one-writer-per-prefix ACL (`deploy/signer-policy.hcl`). A local user holds a scrypt hash; where the customer has an IdP, the record holds an OIDC subject and no secret at all: Alice authenticates against her employer, the signer issues a *domain* token naming her, and the clusters never learn the IdP exists. Per-user UI configuration — walls, layouts — is an object (`users/<id>/prefs`), last write wins with a revision so a stale tab is *told*.

Then the test that is the point of the step. After the domain agent has synced into the south cluster:

```
domain vars:                 ['domain/signer', 'identity/users/alice']
south vars after agent sync: ['domain/keys']
```

Nothing about Alice is in south. What arrived is the signer's public key set, (when there is one) the revocation list, and (Step 5) south's grants. `DomainAgent` is one small Nomad job per cluster (`deploy/agent.nomad.hcl`) whose only right is to write `domain/*` in that cluster's Variables — the way the controller's only right is `vms/*` and a worker's is its epochs and its slot. The test then makes the agent try to write `vms/cameras/7`, `vms/epoch/7` and `vms/slots/w-0` and gets `Forbidden` each time. The cluster's console and gateway read the key set from their **own** cluster's Variables (`ClusterTrust`), never from the domain; a worker reads none of it.

And the identity set is published object-first, then a pointer: `IdentityStore.publish()` writes the whole set as one object, then moves the pointer — `identity/pointer → identity/rev-N` — on a floor. Losing the domain cluster loses users only back to the last publication, and `IdentityStore.restore()` on another cluster is М11's restore with different nouns: the backed-up signer key, then the object the pointer names. The RPO for users is the publication interval, and it is stated.

## Step 4 — The domain is down

```
domain down, agent.sync(): False | south's console still authorises: alice
new login: Unreachable
```

The agent stops updating and writes nothing. The cluster keeps verifying with the keys it has — a signature check needs no network. Issued tokens run to their expiry. Nobody *new* logs in, because the signer is behind the link that is down. That is the bounded outage the services table promised, with the mechanism in front of you: the only thing the domain's absence removes is the issuing of new tokens, and Lesson 7's arithmetic — certificate and token lifetimes chosen from the outage you must survive — is what decides how long that is tolerable.

## Step 5 — Grants are cluster-local, carried by the agent, and expiry is the revocation mechanism

Each cluster holds *subject X may do Y on camera Z until T* in its own Variables under `domain/grants`, and its console and gateway hold them in memory (`ClusterGrants`). The signer publishes each cluster's grants under `domain/grants/<cluster>` in the domain cluster; the agent copies its own cluster's home with the keys. Enforcement is a local read — no lookup, no token exchange — which is the only way authorization survives the domain being down. It also partitions privilege: a compromised cluster's grants are that cluster's, where a central store compromised is total. Workers are not in this picture at all: nothing about a user, a grant or a token ever reaches one, because the console and the gateway are the only things that talk to people, and they are the ones that enforce.

```python
class ClusterGrants:
    def may(self, subject, capability, camera, now=None) -> bool: ...        # a dict lookup and a comparison
    def renew_from_domain(self, renewals): ...                               # what the agent carried: replace, so a dropped grant is not renewed
    def access_ends(self, subject, token_exp) -> float: ...                  # state it in advance
```

Now the asymmetry that makes rights different from configuration:

| If the write does not reach the cluster | Result | Visible? |
|---|---|---|
| A camera edit | records the old way | **Yes** — you can see it |
| A **grant** | the operator cannot get in | Yes — they complain |
| A **revoke** | **the removed administrator keeps the site** | **No** — and they have every incentive not to mention it |

Configuration staleness is benign and self-announcing. Revocation staleness is silent and adversarial, and its window is *unbounded* — until the agent reaches that cluster again, which may be weeks. A grant carrying `valid_until`, renewed by the same agent pass that carries the keys, converts that into **a number the product states**: a cluster whose agent cannot renew lets its grants lapse.

Two lifetimes, and they are not independent:

| | Too short | Too long |
|---|---|---|
| **Token** (`TOKEN_LIFETIME`, 15 min) | Alice is logged out mid-incident and cannot re-authenticate if the domain is unreachable | a revoked employee keeps working until it expires |
| **Grant** (`GRANT_LIFETIME`, 24 h) | a site in a long outage locks out its own operator | a revoked administrator keeps the site |

A token outliving its grant is harmless — the cluster finds no grants and refuses. A grant outliving every token is harmless — nobody can present a subject. The failure is assuming one covers the other. The revocation window is **the shorter of the two**, and most people answer the token:

```
access_ends (revoke cannot reach south): 900.0 s      window: 900.0
```

The test states that number with `access_ends()` before touching the clock, then advances the clock past it and shows `authorise()` refusing — *expired* — with the grant still in the table. Then the other direction: a fresh token after the grant lifetime, refused — *no view grant* — until the upward stream renews what the domain still grants and drops what it does not.

## Step 6 — Wire it into the console

Lesson 3 left `ConsoleAPI(verifier=None)`. The verifier is three lines:

```python
def verifier(token) -> str:
    ks = ClusterTrust(cluster.vars).keyset()       # from THIS cluster's Variables, put there by the agent
    return verify(token, ks, trust.revoked())["sub"]
```

and the console's responses stop saying `"authenticated": false`. The domain's console verifies the *token*; the **owning cluster's** console decides the *grant* when the forwarded edit arrives, from its own Variables, because the domain's console cannot survive the domain being down either and must not be where enforcement lives. The live gateway (Lesson 3) does the same: relays, and the cluster's authoriser decides at the worker's endpoint.

## Step 7 — The honest residue: break-glass

Alice is on site, the uplink is down, and her token expired an hour ago. No amount of design removes that case. A local emergency account is what real products ship, and it reintroduces exactly the password hash this lesson removed. The defensible version is `BreakGlass`: **one** account, audited on every use (success *and* attempt), alarmed on, and rotated after — and a module that says this out loud rather than pretending the clean design has no edge.

```
BREAK-GLASS used by carol: uplink down, token expired
audit: [{'at': ..., 'who': 'carol', 'why': ..., 'ok': False}, {'at': ..., 'who': 'carol', 'why': ..., 'ok': True}]
```

The token it issues carries `via: break-glass` and `who: carol`, so a cluster's grant check can treat the subject `break-glass` differently and the events say who was holding it.

**Deliverable:** grant an operator rights in a cluster, then revoke them while that cluster is unreachable — and state, in advance (`access_ends()`) and then by measurement (the clock), exactly when their access ends. Then delete М9's `operators` rows everywhere and show that Alice still logs in.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `UnknownKey` at one cluster's console | The agent there has not synced, or its token lacks `domain/keys` write. `nomad var get domain/keys` in that region. |
| `UnknownKey` after a key rotation, on old tokens only | The previous key's overlap has ended (`retire:` in the key set). Expected after the overlap; a token older than the overlap was already past its own expiry. |
| Alice can log in but every cluster refuses her | She has a token and no grants. Grants are published per cluster and carried by its agent; a new user has none anywhere. Correct. |
| A revoked administrator still has access in one cluster | That cluster's agent has not renewed its grants — the domain is unreachable from there. Their access ends at `access_ends()`; if that number is a week, the number is the bug, not the cluster. |
| `Expired` immediately after issue | Clock skew between signer and cluster beyond `verify()`'s 60 s tolerance. Lesson 7 names this; NTP fixes it. |
| The identity restore refuses | The pointer names an object the backup store does not hold — publication order broken, or the backup did not copy the latest object. Refuse to guess; restore the previous revision explicitly. |

## Recap

- Channel (mTLS from the domain's root), then caller (a token), then a **local** check (grants) — and only the last two need to survive the domain being down, and both do.
- The token names the subject and nothing else. Clusters hold a public key set, never a hash; workers hold nothing.
- Users live in `identity/*` in the domain cluster's raft, published object-first; nothing about them reaches a worker. The agent carries the key set, the revocation list and the cluster's grants into `domain/*` of every cluster and can write nothing else.
- Grants are cluster-local with `valid_until`; expiry is the revocation mechanism; the window is the shorter of the two lifetimes, stated, then measured.
- М9's `operators` table is superseded. Break-glass is one account, audited, alarmed, rotated — and admitted.

## Exercises

1. Set `TOKEN_LIFETIME` to four hours and `GRANT_LIFETIME` to fifteen minutes. Re-run the window test, state the number, and say which operator you have just locked out during a long outage.
2. Put roles into the token and remove `ClusterGrants`. Then make the domain unreachable and revoke Alice. When does her access end?
3. Give the agent write on `vms/*` "for convenience" and describe the first thing a compromised agent does.
4. The IdP is down but the domain is up. Who can log in? Now the reverse. Write both answers as one sentence each for the datasheet.
5. Design the alarm for break-glass: where it goes, who acknowledges it, and what "rotated after" means when the person who used it is the one who would rotate it.

## Where this is going

The server still authenticates on the channel with the credential someone typed in Step 1. [**Lesson 5**](05-packaging-updates-and-the-licence.md) first settles how the domain ships and updates itself and what a licence does at this level; then [**Lesson 6**](06-secure-introduction-a-box-joins-the-domain.md) replaces that typed credential with one the box earns.
