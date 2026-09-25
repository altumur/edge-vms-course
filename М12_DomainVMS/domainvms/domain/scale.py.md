# scale.py — Lesson 11: the Meter — calls counted and link time charged per member, so the domain's stated limit is measured

**Role in the module.** The module promised a read view for "low hundreds of clusters" and never measured it; cameras as members reach it on the first site. `Meter` wraps each member's two stores, counts every call, and charges it what it would cost on a link — `latency` for an answer, `timeout` for learning there is none. `pass_time(lanes)` spreads the charged work over `lanes` readers. The numbers in Lesson 11 (1 202 calls and 172 KB a pass at 300 members; 82 s in turn with 30 off, 5.1 s in 16 lanes, 1.4 s backing off; 28 550 calls for fifty `where`s through the directory) come from `tests/test_lesson11_hundreds.py` through it.

## `class _Metered` — one store behind the meter: `get`, `list`, `put` counted and charged; bytes returned by object gets counted.

## `class Meter`
### `__init__(self, latency=0.02, timeout=2.0)`, `reset(self)` — `calls`, `cost`, `bytes`, per member.
### `wrap(self, c) -> Cluster`, `wrap_all(self, fed) -> Federation` — the same members, metered.
### `total_calls`, `total_bytes` — sums.
### `pass_time(self, lanes=1) -> float` — longest member first onto the least loaded lane; one lane is the sum, the pass as Lesson 3 wrote it.

## Notes
- It is arithmetic, not a clock. `test_lanes_are_real_threads_not_only_arithmetic` checks the read view's thread pool against stores that really sleep.
