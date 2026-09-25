# Lesson 15 — A Domain Cluster of One Node

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** the domain's services hosted on a camera, holding a **term**; a signed backup of the domain's state kept by other members, carried by their agents; re-hosting from the signer's key and the newest backup any member holds, as an ordinary operation; members that follow the larger term and never carry a smaller one; and an old host that comes back, steps down, and lists what it alone held instead of losing it or applying it.
**Time:** ~120 minutes.

## Why this lesson exists

A site of cameras and no server still has a domain: someone signs tokens, holds the grants, keeps Lesson 9's edits, publishes Lesson 12's settings. This module decided long ago where that runs — in **one designated cluster**, with nothing to fail over to, its key backed up beyond it (*The domain services* in the module design; Lessons 4 and 7). The alternative — the domain's services replicated across clusters by consensus — is raft spanning clusters, which the module refused in its first paragraph.

On a server room, re-hosting the domain was a drill: done once a year, by a runbook, on the day the designated cluster burned. On a site of cameras the designated cluster is **a camera**, and cameras are unplugged by electricians, worn out by their flash, and stolen. Moving the domain has to become ordinary — something an operator does from any other camera's page without a runbook — and three things follow from that, each of which the brief anticipated under the name "primary camera":

- **A term.** Every re-host takes a larger number, and a host that comes back after being replaced must step down rather than split the site. The brief: *two primaries are resolved as everywhere in the platform — the larger number wins, and the loser learns it on its first read.*
- **State beyond the host.** Whatever the domain alone holds dies with its camera unless it was published. Lesson 9's exercise 4 asked where the kept edits go; this is the answer.
- **Nothing lost silently.** The brief again: an old primary's changes that did not propagate *are not silently lost: the console shows them as "not in term N+1 — apply again?"*

> **What you can verify without hardware.** Everything, in `tests/test_lesson15_domain_of_one.py`: four cameras from Lesson 10, the domain on one; an edit kept for a camera that is off; a backup carried by two others; the host dying and the domain re-hosted with the edit; the old host returning; a forged backup; a host restored from the wrong key; and two re-hosts in a month.

## Prerequisites

- **Lesson 4** — the signer, the key set, the agent and `domain/*`.
- **Lesson 7** — the signer's key backed up beyond the domain cluster, and restored.
- **Lesson 9** — the edit kept for a member that is off.
- **Lesson 10** — a camera as a cluster of one; its durable store.
- **Lesson 12** — a signed document behind a pointer, carried by agents, ordered by `(term, rev)`.
- **М10A Lesson 1** — the epoch: a number from one issuer that only grows, the loser finding out on its next read.

## Learning objectives

1. Hold the domain with a term, and make a returning host step down by reading, not by being told.
2. Say exactly which of the domain's state lives only on the host, and publish it beyond the host.
3. Re-host from the signer's key and the newest verified backup, with a term larger than any member has seen.
4. Carry the host record so that it never goes backwards.
5. Show the returning host's un-backed-up changes to a person.

---

## Step 1 — The host holds a term

The host writes a record into its own Variables — `{term, host, at}`, signed by the domain's key — and every member's agent carries it home, as it carries the keys:

```python
    def claim(self) -> None:
        doc = sign({"term": self.term, "host": self.name, "at": self.wall()}, self.signer.tokens)
        _, idx = self.vars.get(HOST)
        self.vars.put(HOST, {"doc": json.dumps(doc, sort_keys=True)}, cas=idx)
```

The term is the epoch one level up. A worker holds an epoch for a camera; the host holds a term for the domain. Neither is given out by a vote — there is nothing to vote with, because no raft spans clusters. The operator's re-host takes the next number, and the number decides.

## Step 2 — What only the host holds

Most of the domain's state already lives on members, because agents put it there. Each cluster's grants are in that cluster. The shared settings are on every member. The keys are on every member. If the host camera dies, all of that survives it.

Not everything. **Lesson 9's kept edit for a camera that is off** is, by definition, on no camera that could carry it home: its cluster is the one that is off. Nor is the crossing decision of Lesson 13, or the mirror plan of Lesson 14, or the grants of a member that has been off since they changed. So the host publishes its state beyond itself, the way Lesson 12 publishes settings — one signed document in the host's durable store, a pointer for each member chosen to keep it, carried home by that member's agent:

```python
    def backup(self, targets: list[str], objects) -> int:
        self.guard()
        self.backup_rev += 1
        doc = sign({"term": self.term, "rev": self.backup_rev, "host": self.name, "at": self.wall(),
                    "state": self.export()}, self.signer.tokens)
        ...
```

`export()` is every item under `EXPORTED`: pending edits, grants, crossings, source books, the mirror plan, the settings pointer. The agent's carry is Lesson 12's `carry`, called with other names — `domain/backup/<member>` in the host, `domain/backup` in the member.

What is **not** in the backup is the signer's key. It is the one thing that must never sit beside the rest: whoever holds a backup and the key can be the domain. It stays where Lesson 7 put it — offline, or in the recovery file the installer handed over.

## Step 3 — Re-hosting

The host camera dies. From any other camera's page, the operator re-hosts on SN1 with the recovery file:

```python
def rehost(fed, new: str, signer_backup: bytes, domain_id: str, objects_of, wall=time.time):
    signer = Signer.restore(domain_id, new_vars, signer_backup, now=wall)
    keys = signer.tokens.keyset()
    for name, c in fed.clusters.items():
        ... the largest term any reachable member carries, and the newest backup that VERIFIES
    ... restore that state into the new host
    host = DomainHost(fed, new, signer, top_term + 1, wall)
    host.claim()
```

Three details carry the weight. The backup is chosen by `(term, rev)` among those that **verify** against the restored key — a backup is believed by its signature, as a settings document is. The new term is one more than the largest term **any reachable member** carries, not one more than the dead host's — so a second re-host in the same month takes term 3, and the host from the first re-host, when it returns, cannot tie with the second (the last test). And the report says what the operator is getting: *term 2 on cam-SN1: the domain's state from backup rev 1, held by cam-SN1; anything the old host changed after rev 1 is not here.*

The first test runs the whole story. SN3 is off; the domain on SN0 keeps an edit for it; SN0 backs up to SN1 and SN2; SN0 dies; the domain is re-hosted on SN1 with the edit in its state; SN3 boots, its agent finds the host with the larger term, carries the edit home, and SN3's console applies it.

## Step 4 — Members follow the larger term, and never a smaller one

A member works out who the host is from what it can verify: its own carried record, and the claims reachable members make **about themselves**. The largest term wins:

```python
def find_host(fed, member_vars, keys, now: float) -> str | None:
    best = read_host(member_vars, keys, now)
    for name, c in fed.clusters.items():
        ...
        if not rec or rec["host"] != name:
            continue                                     # a member's CARRIED record is hearsay; only the host's own claim counts here
```

A host that is off is still the host if nobody holds a larger term — the agent waits for it rather than wandering to a smaller one. And a claim signed by a key the member does not trust counts for nothing, which is why a host set up from the wrong key is followed by nobody (the fifth test): losing the signer's key does not lose the site, it loses the ability to move the domain.

The record is carried like the keys with one rule the keys never needed: **it never goes backwards.**

```python
    have = read_host(member_vars, keys, now)
    if have and int(have["term"]) >= int(incoming["term"]):
        return "holding" if ... else "holding a larger term"
```

Without it, an agent still pointed at the old host, when the old host came back, would carry term 1 over term 2 on its member — and undo the re-host one member at a time.

## Step 5 — The old host comes back

SN0 was not dead, only unplugged, and an electrician plugs it back in. It still believes it is the host at term 1. On its next look it reads the members' records, finds term 2, and steps down:

```python
    def check(self) -> bool:
        ...
            if rec and int(rec["term"]) > self.term:
                self.deposed_by = rec
                return False
```

From then on every write it tries is refused with where the writes go: *cam-SN0 held the domain at term 1; cam-SN1 holds it at term 2 — edits go there.* No message told it; it read. That is М10A's fence, the same shape exactly.

## Step 6 — What it alone held

Between its last backup and its death, SN0 kept one more edit — for SN2, which was off. The new term was restored from the backup and cannot have it: it was on no other camera. Two wrong answers are available. Drop it, and an operator's edit vanishes with no trace. Apply it — have the returning host push its state into the new one — and a host that was replaced gets to write into its replacement, which is exactly what the term exists to prevent.

So it is **listed**, for a person:

```python
def stranded(old_vars, restored_state: dict) -> list[tuple[str, str, str]]:
```

returns every exported item on the old host that differs from what the new term was restored from — here, `domain/pending/cam-SN2` with the edit's value. The console shows it as the brief asked: *not in term 2 — apply again?* A person applying it makes an ordinary edit on the new host, which Lesson 9 then keeps and delivers.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| After a re-host, some members still follow the old host | They cannot verify the new host's claim — a different key was restored — or their agents were never pointed at a reachable member. |
| An old host that came back undoes the re-host on some members | The host record is carried without the term check. Never carry it backwards. |
| A kept edit for a camera that was off is gone after re-hosting | The backup left out `domain/pending/`, or no member kept a backup. Export it; choose at least two keepers. |
| A second re-host produced two hosts with the same term | The new term was computed from the dead host's term, not from the largest any member carries. |
| Edits made on the old host just before it died reappeared on their own | The returning host pushed its state into the new one. List it; a person decides. |

## Recap

- On a camera, the domain's host is replaced often; re-hosting becomes ordinary, and needs a term.
- The term is the epoch one level up: larger wins, the loser learns it by reading.
- What only the host holds — kept edits above all — is published beyond it as a signed backup, carried by chosen members.
- The signer's key is never in the backup; without it nothing verifies and nobody follows.
- Re-host: the key, the newest verified backup, a term larger than any member carries.
- The host record never goes backwards on a member.
- A returning host steps down and lists what it alone held, for a person.

## Exercises

1. A network split leaves SN0 (the host) with half the site and SN1 with the other half, and an operator in the second half re-hosts on SN1. What does each half do until the split heals, and what happens in the first minute after?
2. The brief suggests automatic re-hosting: "the live camera with the smallest serial". Without a quorum, show the split that produces two hosts with the same term, and say why this lesson leaves the button to a person.
3. How many members should keep the backup, and which? Weigh flash, the chance that the keepers die with the host (same switch, same power), and Lesson 14's mirror plan.
4. The site grows a server room, which should now host the domain. Write the re-host that moves the domain from a camera to the server cluster — the same operation, or a different one?

## Where this is going

This closes Part two, and with it the module. Every mechanism in the second half turned out to be a mechanism of the first, applied to members that are small, many and often off: the kept edit is the forwarded write that waits; the cluster of one is a cluster that publishes two objects; the shared settings are the identity set's pointer; the crossing is the grants' carry; the alarm list is the directory's honesty; the term is the epoch. The brief that started it proposed a module of its own. It did not need one.
