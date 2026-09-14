# Module 9 — EdgeVMS: Shipping the VMS as an Appliance

The cloud VMS from [Module 8](../М8_KVS_VMS/README.md) runs on a computer somebody administers. This module turns it into one that nobody does — a box in a ceiling void that updates itself over a network, recovers from its own bad updates, and keeps recording when the link goes down.

Nine lessons, one box, in two halves. **Lessons 1–4** ship the VMS as an appliance: an A/B root under RAUC, rollback decided by a health check, Podman under Quadlet, and a spool that survives the uplink. **Lessons 5–9** make the box own its truth: a database holding what it should be, a reconcile loop making it so, fifty pipelines in one process, failure as the feature, and the console — the design that [М11](../М11_ClusterVMS/README.md) puts under a scheduler. The design brief is [`module-design.md`](module-design.md), in two parts — the appliance, then the recorder.

## The thesis

There are **two independent update planes** in any real edge product, and conflating them is how you end up needing an OS flash to change a setting:

| Plane | Question it answers | Tool here | Changes when |
|---|---|---|---|
| **Below** | What operating system is this box running? | RAUC | You ship a new appliance image |
| **Above** | What workload is running on it? | Podman and Quadlet | You ship a new app version |

And a third thing that belongs to neither: **the data**. It must outlive both planes, which sounds obvious and is the source of this module's two most expensive mistakes — container storage left in a rootfs slot, and footage that only existed in flight.

## Lessons

| # | Lesson | You'll be able to... |
|---|---|---|
| 1 | [The Appliance Problem, and Your Test Bench](01-the-appliance-problem-and-your-test-bench.md) | Explain why in-place updates are unsafe on an unattended box; describe A/B slots and what is *not* duplicated; size a data partition from a stated outage requirement; build and boot a QEMU UEFI bench with two independent root filesystems. |
| 2 | [RAUC: Slots, Bundles, and Signatures](02-rauc-slots-bundles-and-signatures.md) | Write a `system.conf` with current keys; build a two-level CA; sign a bundle; **demonstrate** that a wrongly-signed, wrong-hardware, or tampered bundle is refused; install into the inactive slot with the running one untouched. |
| 3 | [Rollback That Actually Works](03-rollback-that-actually-works.md) | Explain why "did it boot?" is not success; implement the try/OK state machine in GRUB; write a health check that reaches *is footage being written*; induce three failures and document each recovery. |
| 4 | [Podman, Quadlet, the Three-Way Boundary, and the Spool](04-podman-quadlet-and-the-spool.md) | Run the VMS as systemd-managed containers; move container storage off the rootfs; provision credentials at commissioning; **watch footage vanish during an outage**, then build the spool that stops it. |
| 5 | [The Database the Cloud VMS Didn't Need](05-the-database-the-cloud-vms-didnt-need.md) | Separate configuration from observation; write a `tstzrange` + GiST schema answering М8's timeline query directly; partition by time and demonstrate why `DELETE` is not retention; split operator-owned from controller-owned columns; write migrations safe to run unattended at boot. |
| 6 | [A Reconcile Loop with Nothing in It](06-a-reconcile-loop-with-nothing-in-it.md) | Write the loop against a `print()`; use `observed_revision >= revision` as the only test of "applied"; explain why polling is correctness and `NOTIFY` only latency; implement backoff **with jitter** and say what the jitter is for; build the lying-cache bug deliberately. |
| 7 | [Fifty Pipelines in One Process](07-fifty-pipelines-in-one-process.md) | Build pipelines from Python; explain where the work actually happens; cross the GIL boundary deliberately and watch the worker fall over; detect a stalled stream without touching a buffer; turn М9's spool into an archive. |
| 8 | [Failure Is the Feature](08-failure-is-the-feature.md) | Reproduce four failures on purpose and assert recovery from each; enforce retention under disk pressure without a scan; bound what a crash loses and prove it; state the fencing rule and the problem it stands in for. |
| 9 | [What the Console Shows, and What Python Stops Being Right For](09-the-console-and-the-rewrite.md) | Answer *is this camera recording?* in one query; keep positions apart from reasons; put a login in front of it; argue the production language split and identify what a rewrite would **not** touch. |

## What each lesson leaves running

Every lesson ends with something you can see working, and each one is the input to the next.

- **01** → one disk, two complete systems, either bootable, chosen by hand at a menu.
- **02** → that menu replaced by a signed artifact, and three refusals proving the signature check is real.
- **03** → the choice made automatically, with a broken update rolling itself back and nobody in the room.
- **04** → the VMS running under systemd across reboots and OS updates, recording through a ten-minute uplink outage with nothing lost.

## What you can verify without hardware

The module is honest about this because a course that pretends otherwise gets found out at the worst moment.

**Runs anywhere:** the whole PKI and signing chain (`openssl` only — the outputs printed in Lesson 2 are real), the spool and its tests (Python only), partition arithmetic, and any shell logic.

**Needs the QEMU bench:** slot selection, RAUC install, GRUB boot logic, rollback, Quadlet.

**Needs real hardware, and no bench can substitute:** a driver that works in QEMU and not on the board you ship. Lesson 3 Exercise 5 is about what that means for your release process.

One correction worth knowing before you try it: **`systemd-analyze verify` cannot check a Quadlet `.container` file** — it does not know the `[Container]` section and ignores the whole thing. Use `podman-system-generator --dryrun` instead (Lesson 4, Step 3).

## Two conventions carried from Module 8

**Every step produces a result you can see.** A partition table you print, a signature that fails in front of you, a boot menu showing its own state variables, ten minutes of footage that is either there or is not.

**Each lesson replaces a stand-in from the one before.** The hand-selected boot menu becomes a signed bundle; the signed bundle becomes a self-healing rollback; the hand-started container becomes a systemd unit. What this module *adds* to that discipline is naming its stand-ins as debts to be collected later — the AWS credentials in Lesson 4 Step 4 are the first of five temporary secrets, and М12 collects them all — four replaced by giving things identities, and one promoted to the customer's permanent root.

## Decision records

Written alongside the module, with their costs attached rather than quietly omitted:

- [RAUC alternatives](rauc-alternatives.md) — SWUpdate, Mender, bootc, systemd-sysupdate, and where each wins. RAUC is taught because A/B slots are legible and its signature verification is unconditional; **bootc may well be the better choice for a product shipping on x86-64 UEFI**, and the document says so.
- [One container per camera?](worker-and-process-model.md) — the process model at 1000 cameras, and why the orchestrator must not own camera lifecycle. Lesson 4's second sidebar sets this up; М11 Lesson 1 breaks the pattern deliberately.

The multi-node half of this module moved to [М11](../М11_ClusterVMS/module-design.md), where recorders are scheduled across servers. A module called EdgeVMS should not build a raft cluster. The orchestrator comparison that shaped it is recorded in [Kubernetes vs Nomad](../М11_ClusterVMS/kubernetes-vs-nomad.md).

## The artifacts, whole

[`edgevms/`](./edgevms/README.md) is the module's one project — the box (Lessons 1–4) and, under `edgevms/recorder/`, the recorder that runs on it (Lessons 5–9). Its first half assembles what the four lessons leave on the box: `bench/build-disk.sh` builds the A/B disk with everything that must be *in the image* installed before slot A is copied to B; `pki/` is the two-level CA and the three `openssl cms` proofs; `rauc/` is `system.conf`, the manifest and a bundle builder with `--broken-kernel`, `--broken-config`, `--rogue` and `--wrong-hardware` for Lessons 2 and 3; `boot/grub.cfg` is the ORDER/OK/TRY state machine; `quadlet/` and `spool/` are Lesson 4. One thing in it is newer than the lessons: **the health check's third row is no longer a stand-in.** When М9's recorder is installed it reads `recorder_camera_silent_seconds_max` from the recorder's own `/metrics`, locally, with the uplink down — so Lesson 3's rollback decision is finally made on *is footage being written*, which is what the lesson said it had to be.

```bash
cd edgevms && pki/make-ca.sh && pki/verify-chain.sh && python3 spool/test_spool.py   # no VM needed
```


---

## Part 2 — The recorder: Lessons 5–9

Lessons 1–4 leave you with an appliance that updates itself safely and keeps recording through an outage — and that has no idea what it is *supposed* to be doing. Every camera is configured by editing a file and restarting a container.

Lessons 5–9 are where the box starts owning its own truth. Five lessons in which `INSERT INTO cameras` causes a camera to start recording, `DELETE` causes it to stop, and nothing sits in between but a loop you wrote.

### The demo Lessons 5–9 are built backwards from

```sql
INSERT INTO cameras (name, rtsp_url, site_id, enabled)
VALUES ('front-door', 'rtsp://10.0.0.41/stream1', 'store-14', true);
```

Within a few seconds, with nobody restarting anything: a pipeline is running, segments are landing on the data partition, and `SELECT name, phase, observed_revision FROM camera_status` says so. `UPDATE ... SET enabled = false` stops it. `systemctl kill worker` loses nothing but the open segment.

If a lesson does not move that demo forward, it does not belong here.

### What you can verify without hardware (Lessons 5–9)

Better than М9's, because almost nothing here needs a bench.

**Runs anywhere:** the whole schema, the partitioning and retention arithmetic, the reconcile loop, the state machine, backoff and jitter, and the console query. The numbers printed in Lessons 1, 2 and 4 are real output — **PostgreSQL 16.13** and plain Python — not estimates.

**Needs GStreamer:** everything in Lesson 7, plus the stall test in Lesson 8. The module ships a probe script rather than asserting figures: `reference/shard-memory-probe.py` produces `B` and `I` on *your* hardware, which is what М11 Lesson 1 needs to size a shard.

Two corrections worth knowing before you start, both found by running the thing rather than reading about it:

- **PostgreSQL has no `DROP PARTITION` statement** — that is Oracle and MySQL. It is `ALTER TABLE … DETACH PARTITION` then `DROP TABLE`.
- **Partition pruning needs a predicate on the partition key.** `span && …` alone opens every partition's index; the bound on `lower(span)` is what prunes.
- **The `revision` trigger must name the operator-owned columns.** `WHEN (OLD.* IS DISTINCT FROM NEW.*)` bumps `revision` on the worker's own status write, and the lag never clears. Found when the code was assembled; Lesson 5 now carries the corrected trigger.

### The code, whole (Lessons 5–9)

[`edgevms/recorder/`](./edgevms/recorder/README.md) is the five lessons assembled into one runnable recorder: the migrations, the reconciler, the GStreamer actuator, retention with all three disk-full policies, the console, the commissioning tools, the Quadlet units, and the test suite Lesson 8 lays out. Its README maps every sentence in the lessons to the line that implements it, and says exactly what was executed where — the reconciler, retention, worker glue and every SQL statement ran; the GStreamer path and the HTTP layer need a bench with `python3-gi` and `asyncpg`.

```bash
cd edgevms/recorder && python3 tests/run.py       # 27 tests, no database, no GStreamer, milliseconds
```

Lesson 9's rewrite argument is made into a number two modules on, where the recorder has become a worker and a controller: [`vmsserver-go/`](../М10_ServerVMS/vmsserver-go/README.md) and [`clustervms-go/`](../М11_ClusterVMS/clustervms-go/README.md) port М10 and М11 whole, the same seventy tests passing, and measure a worker and a controller at idle — 8.9 MB against 21.2 MB, one 6.8 MB static binary against an interpreter and its packages.

### The stand-ins, and where they get collected

The course names its temporary things where they appear rather than discovering them later. This module adds two of the five:

1. М9 Lesson 4 — AWS credentials in a file on the data partition
2. **Lesson 5 — the database password**
3. **Lesson 9 — one hand-provisioned operator account**
4. М12 Lesson 4 — a per-recorder credential, and a self-signed domain CA

М12 collects them all — four replaced, one promoted. The `valid_until` column in Lesson 5's `grants` table is the mirror image: dead code here, present so that М12 *populates* rather than *migrates*.

**And the worker itself is a stand-in of a different kind.** It is the worker's own controller, built in Python because the course has no media worker of its own; in the product that controller lives inside DriverPack, the process that holds the pipeline, and the platform supplies the rest — assignment, fencing tokens, storage, the web tier. What survives the move is the contract this module's tests define: desired persisted and actual derived, `>=` on the revision, backoff with jitter, positions apart from reasons. [`ARCHITECTURE.md` §1.11](../ARCHITECTURE.md) draws the boundary row by row; `recorder/` is the reference implementation the worker's tests are ported from.

### Where the recorder goes

Everything in this module holds because there is exactly one box — one writer, one worker, a convention where М11 needs a fencing token, and one API surface to protect.

[**М11 — ClusterVMS**](../М11_ClusterVMS/module-design.md) adds the second box. A recorder becomes a scheduler allocation that moves between servers, and nothing built here changes — that is the design working. But two instances of one recorder can briefly exist during a failover, and Lesson 8's one-line rule (*on restart, never resume the previous segment*) has to become an epoch the archive itself enforces.

---

## Where this goes

**М9 has no desired state.** You flash an image and containers run; actual state is the only state there is, and this module's entire job is making that replaceable safely.

[**Part 2 of this module**](module-design.md#part-2--the-recorder-lessons-59) introduces the wish: a row saying a camera *should* be recording, and a loop that closes the gap. Its first act is to put an index over the segments Lesson 4 started writing — turning a spool into an archive.
