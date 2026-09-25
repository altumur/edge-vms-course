# Lesson 12 — Shared Settings Without a Database

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** the settings of the *system* — the retention a camera gets when nobody set one, the folder tree, the scenarios between cameras — published by the domain as one signed document named by a pointer, carried by every member's agent into that member's own durable store, verified there against the member's own key set, never replaced by an older one, and resolved as defaults when read, never written into rows.
**Time:** ~100 minutes.

## Why this lesson exists

Some settings belong to no camera. *Keep events for fourteen days unless the camera says otherwise.* *These are the folders: Site 1 / Building A / Floor 2.* *When the gate camera sees a vehicle, turn the yard camera to preset 3.* In a server room these live in the cluster's Variables, written by its console, read by everything in the cluster.

With cameras as members there is no such cluster. Every member is its own; a setting written into one camera's store is that camera's. And the domain has no database — that was decided in [`where-the-database-lives.md`](where-the-database-lives.md), five revisions long, and it is not reopened here.

The brief's answer was a "primary camera" that holds the shared keys and pushes an immutable, signed file of them to every other camera, which keeps the newest valid copy it has seen. Read with this module in hand, that is two things the domain already does: the identity set is **published object-first behind a pointer** (Lesson 4), and trust is **checked offline against a key set every member already holds** (Lessons 4 and 7). This lesson puts the two together and adds what comes from where the copy is *read*: on every member, possibly with the domain gone for a month.

> **What you can verify without hardware.** Everything, in `tests/test_lesson12_shared.py`: a domain and three cameras from Lesson 10; an edit, its delivery report with a camera off; a document signed by a stranger with a perfectly matching checksum; a pointer that goes backwards; defaults resolved against rows that are never touched; two editors; a 500-camera tree over the 64 KiB a Variable may hold; and a camera rebooted with the domain switched off.

## Prerequisites

- **Lesson 4** — publish-then-point for the identity set; the key set agents carry into `domain/keys`.
- **Lesson 7** — keys that rotate with overlap; trust that works offline.
- **Lesson 9** — outcomes reported by the member's agent and read back by the domain.
- **Lesson 10** — a camera as a cluster of one, with flash and RAM.
- **М10A** — the 64 KiB ceiling of a Nomad Variable (`w2cplatform/limits.py`).

## Learning objectives

1. Publish a document too large for a Variable with a Variable as its point of change.
2. Make a copy trustworthy by its signature rather than by where it came from.
3. Order documents by `(term, rev)` so a member never goes backwards.
4. Resolve a shared setting as a default at read time, and say why writing it into rows is wrong.
5. Report delivery per member without rounding the silent ones into a total.

---

## Step 1 — A pointer and an object

The document is an object; the Variable is a pointer to it. The edit writes the object first and the pointer second, and the pointer is written **by CAS** — which is what makes two editors safe:

```python
    def edit(self, mutate, base_rev: int, by: str | None = None) -> int:
        doc, idx = self.current()
        if int(doc["rev"]) != base_rev:
            raise Conflict(f"shared settings are at rev {doc['rev']}; the edit was made against rev {base_rev}")
        ...
        self.objects.put(key, raw)                                                           # 1. the object
        self.vars.put(POINTER, {"object": key, "rev": new["rev"], "term": new["term"],        # 2. the pointer, by CAS
                                "sha256": hashlib.sha256(raw).hexdigest()}, cas=idx)
```

Why not a Variable holding the settings themselves? Because of what they grow to. Five hundred folders and five hundred scenarios are some eighty kilobytes, and a Nomad Variable holds 64 KiB, whole — a constant in the scheduler, not a setting (М10A's `limits.py`). The test builds exactly that tree: the document is over the ceiling, the pointer is under 200 bytes, and the member gets all 500 folders.

## Step 2 — Believed by its signature

The document carries the signer's signature, made with the same Ed25519 key that signs tokens, under its `kid`:

```python
def sign(doc: dict, issuer: TokenIssuer) -> dict:
    return {**doc, "kid": issuer.kid, "sig": _b64(issuer.key.sign(_canonical(doc)))}
```

and a member checks it against **its own** key set — the one its agent carried into `domain/keys` — never one that arrived with the document. That is the whole difference between a copy and a rumour: a copy that verifies is as good as the original wherever it came from, and a copy that does not is nothing, however it arrived.

The test does what an attacker who can write to the domain's object store would do: signs a document with a key of its own, stores it, and points at it with a checksum that matches perfectly. The checksum proves the object is the one the pointer names. It proves nothing about who wrote it. The agent refuses — *signed by key 'a1b2c3d4', which this member does not trust* — keeps the document it had, and writes the refusal into `domain/shared-refused`, where the domain reads it.

## Step 3 — Never backwards

A domain restored from yesterday's backup points at yesterday's revision. A member that already holds today's must not take it. The rule is one comparison, made before anything is fetched:

```python
    have, hidx = member_vars.get(dst)
    if have and (int(have["term"]), int(have["rev"])) >= (int(ptr["term"]), int(ptr["rev"])):
        return "up to date" if ... else "holding newer"
```

`rev` counts edits. `term` is Lesson 15's: the number of times the domain has been re-hosted, and it outranks `rev` — a re-hosted domain starts from the newest backup it can find, which may be a few revisions behind, and its next edit must still win. Until Lesson 15 the term is 1.

## Step 4 — Kept where it is read

The agent carries the document home — object first, then the member's copy of the pointer, so a member that dies between the two still points at a document it has — into the member's **durable** store. On a camera that is its card, not its RAM (Lesson 10): the settings must survive a reboot with the domain unreachable. The member's console reads it through `SharedView`, which checks the signature again on the way in, because a copy on flash is still only data.

The last test is the module's thesis applied to settings: publish, carry, switch the domain off, reboot the camera — and the defaults are what they were.

## Step 5 — A default, not a copy

The tempting implementation of *default retention fourteen days* is to write fourteen into every row that has none. It is wrong three ways. It makes the agent a writer of rows, which Lesson 4 forbade. It turns one change of default into five hundred writes, some to cameras that are off — five hundred edits for Lesson 9 to keep. And afterwards nobody can tell a camera that was *set* to fourteen from one that merely inherited it, so the next change of default cannot know which rows to change.

So a shared setting is resolved **when it is read**, and the console says where each value came from:

```python
    def effective(self, row: dict) -> dict[str, tuple[object, str]]:
        ...
            if row.get(k) is not None:
                out[k] = (row[k], "camera")
            elif k in defaults:
                out[k] = (defaults[k], f"domain rev {doc['rev']}")
```

The test sets thirty days on one camera's own page, publishes a default of fourteen, and checks both answers — `(14, "domain rev 1")` and `(30, "camera")` — and that no row's revision moved.

## Step 6 — Delivered to how many

Accepted is not delivered, as Lesson 9 said of edits. The console shows beside the settings which members hold which revision, read from each member's own copy of the pointer — the agent's report:

```
rev 1 on 2 of 3 members; not answering: cam-SN2
```

A member that is off is named, not rounded into the total, and a member that refused is named with its reason. When SN2 comes back, its agent takes the document on its first pass and the sentence becomes *rev 1 on 3 of 3 members*.

## Step 7 — Scenarios between cameras

A scenario — *vehicle at the gate, yard camera to preset 3* — is shared data like the rest: it is in the document, and every member has it. Each member runs the scenarios whose trigger is its own event. The action is on **another** member, and there the shared document stops helping: turning the yard camera is a request to that camera's cluster, forwarded through the domain as Lesson 3 forwards an edit.

And unlike an edit, it is **not kept** when the target is off. *Preset 3 now* is worth something now; delivered an hour later, when the camera boots, it points the camera at an empty yard for no reason anybody remembers. A request carries a deadline and dies at it; an edit carries a value and waits. That difference is the exercise below.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Publishing the folder tree fails with a size error | The settings are in a Variable. Put them in an object and point at it. |
| A member took a document written by someone without the key | The signature is checked against a key set that came with the document, or not at all. Check against the member's `domain/keys`. |
| After restoring the domain, members lost last week's settings | The agent replaces what it holds with whatever the pointer names. Compare `(term, rev)` first. |
| Changing a default takes minutes and leaves some cameras behind | The default is written into rows. Resolve it at read time. |
| The console says "delivered" while a camera is off | The report counts responses, not members. Name the silent ones. |

## Recap

- Shared settings are one object behind one pointer; the pointer is the CAS, the object has no size ceiling worth naming.
- Signed with the domain's key and verified against the member's own key set: a copy is believed by its signature.
- `(term, rev)` only grows on a member, whatever the domain points at today.
- Kept in the member's durable store and read there, with the domain gone.
- A default is resolved at read time and never written into rows; the console says where each value came from.
- Delivery is reported per member, silent ones named.

## Exercises

1. Write a forwarded request for *preset 3 on the yard camera* with a deadline. What does the domain answer when the yard camera is off, and why is that different from Lesson 9's answer for an edit?
2. The signer rotates its token key (Lesson 7). A member has been off for longer than the overlap. What happens to the next shared document it is offered, and what must the agent carry first?
3. Two customers' sites share a folder tree template but not their cameras. Design the document so each domain can publish its own tree without either one being able to change the other's.
4. A camera's own page shows its effective retention. Write the sentence it shows for a value inherited from the domain, including the case where the camera has never received a document at all.

## Where this is going

Settings cross from the domain to every member. [**Lesson 13**](13-a-stream-from-another-cluster.md) sends something the other way and sideways: a server room's recorder recording a camera that is a cluster of its own — footage crossing between clusters while the work stays where it is.
