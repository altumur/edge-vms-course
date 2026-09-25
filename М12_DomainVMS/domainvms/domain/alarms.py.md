# alarms.py — Lesson 14: one list of alarms from every member, and a neighbour's copy of each camera's closed alarm buckets

**Role in the module.** The brief's `MergedIndex` over cameras and its mirrors, which had no lesson in the first plan. The domain merges each member's alarms from its door, newest first, a page per member, with what it could not reach said (Lesson 1's honesty). A neighbour keeps a copy of another member's CLOSED alarm buckets — immutable, so copying needs no coordination — pulled by the keeper; who keeps whose is the domain's decision (`domain/mirrors/<member>`, carried by the agent), by rendezvous hashing so it barely moves as the site grows. A member answered from a copy says up to when the copy knows.

## Module-level names
- `BUCKET = 600` — М10A's bucket span.

## `class Card`
A member's events on its card, as М10A's buckets (`EventLog`, `buckets_under`, `read_bucket`), and a `mirror/<member>/…` tree for copies. `observe(epoch, t, kind, alarm=False)`; `alarms(since, until, limit)` → `{events, truncated}`; `closed_alarm_buckets(now)` → `{path: alarm lines}` for buckets whose span ended; `keep_copy(of, path, lines)` → False if already there, else write-and-rename; `mirrored(of, since, until, limit)` → `{events, truncated, known_until}`.

## `class EventDoor` — a member's `Card` through its door; `Unreachable` when off.

## `class MirrorPlan`
`choose(members)` → `{member: [keepers]}`: the others ranked by a hash of the pair, top `copies`, from the same network when enough share one. `publish(fed)` writes `domain/mirrors/<keeper>` = `{member: "1"}`. `holders(of)` inverts it.

## Functions
### `mirror_once(me, my_vars, doors, now) -> int` — for each member in this one's carried `domain/mirrors`, copy the closed alarm buckets not yet held; a silent source is skipped.

## `class DomainAlarms`
`list(since, until=None)` → `{events (each with member, and from_mirror_on when copied), members: {name: {state ok|mirror|unreachable, via, known_until, truncated}}, complete, sentence}`.

## Notes
- `test_who_keeps_whose_copy_is_stable_as_the_site_grows`: one pair of 300 moves when a camera is added. The plan's load is uneven (some keep none, a few keep four) — the lesson's first exercise.
