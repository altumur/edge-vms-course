# Lesson 9 — An Edit for a Cluster That Is Off

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** a place for the domain to keep an edit when the owning cluster does not answer — per field, beside the grants — carried home by that cluster's agent when it is back, applied by the cluster's own console as the operator who made it, and reported back so the domain can clear what landed and show what did not.
**Time:** ~120 minutes.

## Why this lesson exists

Lesson 3's write API forwards an edit to the owning cluster and, when that cluster does not answer, says `503`: *could not look*. That was honest, and for a server room it is also enough. A cluster is several servers; for the whole of it to be unreachable is an incident, rare and noticed, and an operator who gets `503` is looking at a real outage.

Now count a cluster that is **one device**. A camera running the platform is a cluster of its own — one store on its flash, one writer, nothing to fail over to — and a camera is off often, for reasons that are not incidents: its power, the PoE switch it hangs from, a maintenance window, a remote site on a link that sleeps. And cameras are edited in bulk: *retention thirty days for everything in the Perimeter folder*. Fifty cameras, three of them off, and the edit is `503` three times, which means an operator who has to remember three names and come back.

So the domain keeps the edit. The rest of the lesson is what "keep" has to mean for that to be safe, because the cheap versions of it are wrong in ways that do not show up until somebody asks why a camera has the setting it has.

> **What you can verify without hardware.** Everything, in `tests/test_lesson9_pending.py`: a camera that goes off, an edit kept for it, the camera coming back and taking it from its own agent, a field changed on site meanwhile, a grant revoked meanwhile, the same edit carried home twice, and an edit made before the previous one was confirmed. The clusters are Lesson 1's fakes with a link you can pull.

## Prerequisites

- **Lesson 3** — the write API, the owner it forwards to, and the `503` this lesson replaces.
- **Lesson 4** — the domain agent: one small job per cluster whose only right is `domain/*` in that cluster's Variables, carrying what the signer publishes under `domain/grants/<cluster>`. This lesson is one more thing it carries.
- **Lesson 1** — an answer that knows it is incomplete, and the difference between *not found* and *could not look*.
- **М10 Lesson 6** — the controller's CAS on a row, and why a row has exactly one writer.

## Learning objectives

1. Say why `503` is right for a server room and wrong for a camera, without changing who owns anything.
2. Keep an edit where the domain keeps everything a cluster needs from it — with no database.
3. Keep it **per field**, against the value last seen, merged rather than queued.
4. Apply it in the cluster, by the cluster's console, as the operator, with that operator's grant checked *then*.
5. Tell *applied*, *already there* and *conflict* apart, and show the third rather than decide it.
6. Match an outcome to the edit it was about by version, never by clock.

---

## Step 1 — Keep it, without changing the owner

The first design anyone reaches for is to move the row: while the camera is off, let the domain — or the server cluster — hold the camera's settings, and have the camera pull them when it is back. That is two writers of one row over time, a stream of changes to carry them, and the camera writing into somebody else's store to report. Every piece of the platform so far is built on the opposite rule.

So the owner does not change. The camera's console remains the only writer of the camera's rows. What changes is only that the domain, instead of refusing an edit it cannot deliver, **holds it until it can**, and the cluster applies it the way it applies any other edit — through its console.

```python
    def update_camera(self, camera, fields, idempotency_key, token=None):
        ...
        ans = self.directory.where(camera)
        if not ans.found:
            kept = self._keep(camera, fields, subject, ans)
            if kept is not None:
                self._seen[idempotency_key] = kept
                return kept
            raise ApiError(404 if ans.complete else 503, ans.sentence())
```

`_keep` keeps an edit only for a cluster that **did not answer**, and only when the domain knows it is that cluster's camera. A complete answer that found nothing is still `404` — the camera is not anywhere. A camera missing from a cluster that *did* answer is gone, not waiting. And a domain with nowhere to keep edits answers exactly as Lesson 3 did — `503` — rather than pretend.

Where does the domain know the owner from, when the owner is silent? Not from the directory: Lesson 1's directory never copies rows, and a silent cluster's cameras are simply not found. From the **read view**, which keeps a silent cluster's last snapshot and says how old it is. `ReadView.last_known(camera)` is that copy, and it is also the value the edit will be measured against.

## Step 2 — Where it lives: beside the grants

The domain has no database, and that was a decision, not an omission. It does not need one here either, because it already has a pattern for *something the domain decides, that a cluster needs, carried by the cluster's agent when the cluster can be reached*: the grants. The signer writes `domain/grants/<cluster>` in the domain cluster's Variables; the cluster's agent copies it into its own.

A kept edit is the same shape:

```
domain/pending/<cluster>      in the domain cluster     one item per camera, by the domain's name for it
domain/pending                in the member cluster     the agent's copy, carried home
domain/outcomes               in the member cluster     what happened, written by the agent
```

The agent's right does not grow. It writes `domain/*` in its own cluster and nothing else — it does not write the camera's row. It carries the edit home, hands it to the cluster's console, and writes down what the console did.

## Step 3 — Per field, against the value last seen

A kept edit is not the row the operator saw with the new values pasted in, and it is not a list of edits to replay. It is, **per field**, two values:

```json
{"events_retention_days": {"old": 30, "new": 7}, "name": {"old": "gate", "new": "main-gate"}}
```

`old` is what the domain last saw — the read view's copy. `new` is what the operator wants. That pair is what makes it safe to apply later, and the next step is why.

Two edits made while the camera is still off **merge**: the camera needs the last value of each field, not a history. The field keeps its `old` — while the camera is off nothing new arrives to move it — and takes the latest `new`.

## Step 4 — Applied, already there, or a conflict

The camera comes back. Its agent carries the edit home and, field by field, compares it with what the camera holds now:

```python
        for f, d in e["fields"].items():
            cur = row.get(f)
            if cur == d["new"]:
                already.append(f)                            # carried home twice before the domain cleared it
            elif cur == d["old"] or cur in d.get("via", []):
                apply[f] = d["new"]                          # what the edit was based on, or a value the domain sent before
            else:
                conflicts[f] = {"old": d["old"], "new": d["new"], "current": cur}
```

- **Still `old`** — nothing happened to that field while the camera was off. It takes `new`.
- **Already `new`** — the edit was carried home once already and the domain has not cleared it yet. Nothing is written. Carrying it twice applies it once.
- **Anything else** — somebody changed that field on the camera while it was off, on the camera's own page, which is still its console. The domain's edit was based on a value that is no longer there. That is a **conflict**, and it is shown, not decided.

Why not the two simpler rules. *Last writer wins* would overwrite the change made on site — a change nobody at the domain ever saw — and nothing would record that it had been there. *Compare the whole row* would refuse the whole edit because the name moved, throwing away the retention change for the sake of a field it never touched. Per field is the rule that loses neither.

## Step 5 — As the operator, with the grant checked then

The console applies the edit **as the operator who made it** — the kept edit carries the subject — not as the agent. The row has one writer, and the edit is that person's.

An edit can wait a week, and a week is long enough for an operator to lose the right to make it. So the grant is checked **when the edit is applied**, by the cluster's console against the cluster's own grants — exactly the check a live edit gets. A refusal is recorded as a refusal and kept on the domain with its reason. It is not retried as if the network were at fault, and it is not forced.

## Step 6 — How it comes back: by version, never by clock

The agent writes what happened into its own cluster's `domain/outcomes`. The domain reads that the way it reads a snapshot — through the cluster's stores — and folds it in: applied and already-there fields go, a conflict stays annotated with what the camera holds, a refusal stays with its reason, a camera the cluster no longer has takes its edit with it.

The outcome must be matched to the edit it was about, and the obvious key is wrong. The agent's clock is the camera's; the domain's is the domain cluster's; comparing the two decides nothing. Each camera's kept edit has a **`rev`**, bumped by every change to it, and the outcome names the `rev` it applied. Only an outcome for the current `rev` clears anything. An old report can never clear a newer edit.

## Step 7 — An edit made before the last one was confirmed

There is a race between the two sides, and it is the one this lesson's tests found in its own first design. The camera takes an edit and reports it; the domain reads the report later. In between, the camera can go off again, and the operator can edit the same field again. The merged edit is still based on the value from before the *first* edit — but the camera took the first edit, and now holds a value **the domain itself sent**. Read by Step 4's rule as it stood, that is somebody else's change: a conflict about nothing.

So a field remembers the values it has already wanted — `via` — and a camera holding one of them holds a base, not a surprise:

```python
                elif have["new"] != new:
                    via = have.get("via", [])
                    have["via"] = via if have["new"] in via else via + [have["new"]]
                    have["new"] = new
```

And the report about the first edit, read after the second was made, clears nothing, because its `rev` is not the current one. Both halves are one test.

## Step 8 — Resolving a conflict

A conflict waits for a person. *Apply again* means apply against **what is there now**: a new edit to a conflicted field takes the value the camera reported as its `old`. Measured against the original value, it would conflict again for ever.

## Step 9 — Accepted is not applied

A kept edit returns `202`, not `200`, and the response says `pending: true` and why. A bulk edit reports per camera, so the client can say *applied to 47 of 50, 3 waiting*. The domain's list shows a waiting edit with its age. An operator who reads *accepted* must never be able to believe *done*.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| An edit for a camera that is off is refused with `503` | The API was built without `pending=` and `last_known=` — Lesson 3's API. Correct for it; wire both. |
| A kept edit overwrote a change made on the camera | The comparison is not per field, or it compares with `new` only. The field must still hold `old` (or a `via`) to be applied. |
| A camera shows a conflict on a field nobody touched on site | The field was edited again before the previous edit's outcome was read, and `via` is missing — the camera holds a value the domain sent. |
| A newer edit disappeared after the camera came back | Outcomes are matched by time, or not matched at all. Match by `rev`. |
| An operator whose grant was revoked still changed a camera | The grant was checked when the edit was accepted, not when it was applied — or the agent applied it as itself. |
| A cleared edit is applied again and again | The agent does not carry an EMPTY pending set home, so the cluster's copy never clears. Carry it even when there is nothing left. |

## Recap

- `503` is honest and, for a cluster that is one device and often off, useless. The domain keeps the edit — **without changing the owner**.
- It lives beside the grants: `domain/pending/<cluster>` in the domain cluster, carried home by the cluster's agent, which still writes nothing but `domain/*`.
- Kept **per field**, against the value last seen; merged while the camera is off.
- Applied by the cluster's own console, as the operator, with the grant checked **then**.
- *Applied*, *already there*, *conflict* — and the conflict is shown, not decided.
- Outcomes come back through the cluster's own `domain/outcomes`, matched by **`rev`**, never by clock; `via` covers the edit made before the last one was confirmed.
- Accepted is not applied: `202`, per-camera results, age on every waiting edit.

## Exercises

1. Replace the per-field comparison with *last writer wins* and write the support call that follows a month later: which setting, who set it, and why nobody can tell.
2. Match outcomes by the agent's `at` instead of `rev`. Put the camera's clock ten minutes behind the domain's and find the edit that is lost.
3. Remove `via` and run the race test. Then describe, in one sentence, what an operator sees and why they would conclude the site staff are interfering.
4. The domain cluster is itself a camera (Lesson 14). Say where the kept edits live, what is lost if that camera dies, and what the domain must therefore publish beyond itself — then compare with how it already publishes the identity set.
5. An edit waits for a camera that is never coming back. Should it expire? Argue both ways, and say which failure each choice makes silent.

## Where this is going

This is the first of the lessons that treat a device as a member of the domain. [**Lesson 10**](10-a-cluster-of-one.md) builds that member: a camera as a cluster of one, with a unit pinned to its hardware, no orchestrator, and the domain agent as its only link upward. The lessons after it take the domain to hundreds of such members, give it shared settings without a database, let a server's recorder read a stream from a camera's cluster, and host the domain itself on a camera — where the kept edits of this lesson become one more thing the domain must not keep only on itself.
