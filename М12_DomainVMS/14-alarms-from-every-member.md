# Lesson 14 — Alarms From Every Member

**Module:** DomainVMS — the smallest layer above a set of clusters (Module 12)
**You will build:** one list of alarms across cameras that are each their own cluster — fetched from every member's door, merged newest first, each line naming its member, a page per member so one storm cannot hide the rest — and a second copy of each camera's closed alarm buckets on a neighbour, pulled by the neighbour, chosen by the domain, so that a camera which is off is answered from the copy, with the list saying up to when the copy knows.
**Time:** ~100 minutes.

## Why this lesson exists

An operator of a site of cameras watches one thing more than anything else: the list of alarms. *Door forced, gate 3, 14:21. Stream lost, loading bay, 14:25.* Newest first, whichever camera saw it.

Inside a cluster that list exists. Every worker writes its events into buckets on its resource (М10A); М11's `MergedIndex` merges every resource's buckets and says, per source, whether it answered and whether it was cut short. Across members there is no cluster to merge in: each camera's events are on its own card, behind its own door. And there is a harder problem the cluster never had. A server that dies leaves its disk behind, often readable; a resource can be mirrored to another server in the same room (`platform/mirror`). A camera that is off, or whose card has died, takes its alarms with it — and the camera that is off is very often the camera whose last alarm matters most. The camera at the gate goes dark thirty seconds after it reported the door forced.

The brief asked for exactly this — `MergedIndex` over the cameras, and neighbours keeping copies of each other's alarms — and left it without a lesson. This is that lesson. It adds no new principle: it is the directory's honesty (Lesson 1) applied to events, and the platform's mirror applied between members.

> **What you can verify without hardware.** Everything, in `tests/test_lesson14_alarms.py`: three cameras from Lesson 10 with М10A's real `EventLog` writing buckets on a card; the merged list; a camera switched off after its neighbour pulled its closed buckets; a camera with no reachable copy; the copy's rules (closed only, alarms only, twice is once); the mirror plan on a three-hundred-camera site as it grows; and a storm of 150 alarms on one camera beside a single alarm on the next.

## Prerequisites

- **Lesson 1** — an answer that knows it is incomplete.
- **Lesson 10** — a camera as a cluster of one, with a door that is shut while it is off.
- **Lesson 11** — hundreds of members, and what asking each of them costs.
- **М10A** — the event bucket (`<root>/vms/<unit>/e<epoch>/<start>Z.events.jsonl`), traffic classes (`alarm`, `observation`), and a bucket that is *closed* once its span has ended.

## Learning objectives

1. Merge alarms from many members with what could not be reached said as part of the answer.
2. Keep one noisy member from pushing every other off the page.
3. Copy another member's alarms with no coordination, by copying only what can no longer change.
4. Choose who keeps whose copy so that the plan barely moves as the site grows.
5. Answer from a copy without letting "none known since" read as "none".

---

## Step 1 — One list, per member

The domain asks each member's door for its alarms in the window, newest first, **at most a page**, and merges:

```python
            try:
                got = self.doors(name).alarms(since, until, self.per_member)
                members[name] = {"state": "ok", "truncated": got["truncated"]}
                events += [{**e, "member": name} for e in got["events"]]
                continue
            except Unreachable:
                pass
```

Every line names its member. Every member has a state. `complete` is true only if every member answered, and the list carries a sentence — *every member answered*, or what did not — the way `Answer.sentence()` has since Lesson 1.

A member reads its alarms from М10A's buckets on its card: `Card.alarms` walks `buckets_under(root, "vms", unit, …)` and keeps the lines whose class is `alarm`. Observations — motion, every frame's worth of analytics — never leave the card for this list; they are nearly every line (М10A's traffic classes), and nobody watches them.

## Step 2 — A storm is one line of news

One camera raising a hundred and fifty alarms in an hour is a camera with a problem — a flapping contact, a misaimed detector. Merged by time alone, it fills the page, and the quiet camera next to it — the one with the single *door forced* — is on page two.

So each member is asked for at most `per_member` lines, and a member that had more says so: `truncated`. The test puts 150 alarms on SN1 and one on SN0, asks with a page of 100, and finds SN0's alarm on the list, SN1 marked truncated, and the sentence saying *cam-SN1 had more alarms than one page holds; showing its newest*. This is `MergedIndex`'s `truncated` per source, for the same reason.

## Step 3 — A copy that needs no coordination

A neighbour keeps a copy of another member's alarms. Copies are where replication gets hard — two writers, ordering, a copy half-made when the source dies. Here none of that arises, because of one rule: **only closed buckets are copied.**

A bucket is closed once its span has ended (М10A): nothing will ever be appended to it again. A copy of it is therefore either all of it or none of it, and copying it twice is copying it once:

```python
    def keep_copy(self, of: str, path: str, lines: list[dict]) -> bool:
        dst = os.path.join(self._mirror_root(of), path)
        if os.path.exists(dst):
            return False                                 # closed means immutable: what is here is all of it
        ...
        os.replace(tmp, dst)                             # all there or not there
```

The copy keeps **alarm lines only**. A neighbour's flash is not a second card — it could not hold another camera's observations, and should not try. The open bucket is in no copy, by design: that is the accepted loss, the same one the platform accepts for a resource's disk (the last few minutes, stated).

## Step 4 — Pulled by the one who keeps it

The copy is made by the member that keeps it, reading the source's door — never pushed by the source:

```python
def mirror_once(me: Card, my_vars, doors, now: float) -> int:
    keeps, _ = my_vars.get(MIRRORS_PATH)
    for of in sorted(keeps or {}):
        try:
            closed = doors(of).closed_alarm_buckets(now)
        except Unreachable:
            continue
        ...
```

This is the platform's rule since М10B: evacuation pushes, backfill pulls — the initiative is with the one who lacks. A camera does not need to know who keeps its copy, and nothing is written into it. A source that is off is simply not copied this pass; there is nothing to retry and nothing to reconcile.

## Step 5 — Who keeps whose copy

Cameras cannot pick their own neighbours sensibly: a camera knows nothing of the others. The domain does, and it decides, by **reachability** — Lesson 1's criterion for placement — and stores the decision per member, `domain/mirrors/<member>`, carried home by the member's agent: the second per-cluster row the agent learned to carry in Lesson 13.

The choice is rendezvous hashing: each member ranks the others by a hash of the pair and takes the top `copies`, among the members on its own network when there are enough of them:

```python
            near = [b for b in others if nets & members[b]]
            pool = near if len(near) >= self.copies else others
            out[a] = sorted(pool, key=lambda b: -_score(a, b))[: self.copies]
```

Its virtue is what happens when the site grows. Three hundred cameras on three networks; add one. A pair changes only where the newcomer outranks the current holder — in the test, **one** pair out of three hundred. A plan that reshuffled on every change would re-copy every camera's alarms across the site's links each time a camera was added.

## Step 6 — Answered from the copy, and honest about it

SN0 raised *door forced* twenty-five minutes ago and again ninety seconds ago, and then went off. Its neighbour SN1 had pulled its closed buckets. The list:

- has the first alarm, marked `from_mirror_on: cam-SN1`;
- does **not** have the second, which was in SN0's open bucket and in no copy;
- says so: SN0 is in state `mirror`, `known_until` the end of the last closed bucket, and the sentence reads *cam-SN0 off — its alarms from the copy on cam-SN1, known up to 13:40 UTC; none known since*.

That last clause is the lesson. A list that showed SN0's copied alarms and nothing else would read as *SN0 has been quiet since 13:40* — the most dangerous thing it could say about a camera that went dark. And a camera whose copy cannot be reached either is `unreachable`, said by name: *cam-SN2 off, and no copy of its alarms could be reached.*

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| One camera's alarms fill the page and others vanish | Merged by time with no per-member page. Ask each for a page and mark `truncated`. |
| A mirrored bucket is missing its last alarms | An open bucket was copied. Copy only closed buckets. |
| Neighbours' flash fills up | Whole buckets are copied, observations included. Alarms only. |
| Adding one camera re-copies alarms site-wide | The plan is recomputed with a scheme that reshuffles (modulo, sorted order). Rendezvous hashing. |
| An operator concludes a dark camera was quiet | The list shows copied alarms without `known_until`. Say up to when the copy knows. |

## Recap

- One list from every member's door, each line naming its member, the unreached said as part of the answer.
- A page per member, and `truncated`: a storm is one line of news, not the whole page.
- Neighbours keep copies of closed alarm buckets only — immutable, so copying needs no coordination.
- Copies are pulled by the keeper, never pushed; nothing is written into the source.
- Who keeps whose copy is the domain's decision, by reachability, stable as the site grows.
- A member answered from a copy says up to when the copy knows; "none known since" never reads as "none".

## Exercises

1. The test's plan leaves about a third of the cameras holding no copy and a few holding four. Bound each keeper's load at two without breaking stability, and measure how many pairs move when a camera is added.
2. Two copies per camera instead of one. When is the second worth its flash — and what does the list do when both copies disagree about `known_until`?
3. A camera's card dies and the camera stays on. What does the list show for it, and what should it show?
4. The list is fetched from three hundred doors per refresh. Using Lesson 11's numbers, how often can the console refresh it, and what would you change first — the page size, the lanes, or pulling only since the last refresh?

## Where this is going

Every piece of the domain now runs across cameras. One piece still runs *on* something: the domain's own services, which a site with no server has to put on a camera. [**Lesson 15**](15-a-domain-cluster-of-one-node.md) does that, and makes moving them — when that camera dies, as cameras do — an ordinary operation.
