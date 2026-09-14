# One Container Per Camera? The VMS Process Model

**A decision record for М9_EdgeVMS and М11.** Companion to [`kubernetes-vs-nomad.md`](../М11_ClusterVMS/kubernetes-vs-nomad.md) and [`rauc-alternatives.md`](rauc-alternatives.md), written in answer to "if one server handles 1000 cameras, is that 1000 Podman containers — and another 1000 for recording?"

No to both. The interesting part is *why*, because the usual reason given is the wrong one.

---

## Verdict

1. **Container-per-camera fails on lifecycle, not overhead.** The overhead is real but survivable. The lifecycle coupling is not.
2. **Recording is not a second process.** It is a branch of the pipeline that already owns the camera connection.
3. **Two supervisors, not one.** The orchestrator manages *workers*; the cluster's placement manages *cameras*. Merging them is the mistake this record exists to prevent. *(Written when the course still had a "domain controller"; the second supervisor turned out to be a stateless placement function in М11 Lesson 5, and the name was retired.)*

---

## 1. What a container actually buys here

Podman containers are cheaper than people fear. Namespaces and cgroups cost kernel structures, and 1000 containers from one image share the overlayfs page cache — so libgstreamer's text pages *are* shared. The costs that bite at 1000× are the bookkeeping ones:

- one `conmon` supervisor per container — 1000 extra processes
- 1000 systemd units — dependency resolution, journal pressure, boot measured in minutes, `systemctl` unusable
- 1000 Nomad allocations on one client — heartbeats, task-runner goroutines, log shipping, raft state

Survivable, but pointless, because look at what the container abstraction is being spent on:

| What a container gives you | Worth it at 1000 cameras? |
|---|---|
| **Packaging** — an image with its dependencies | **No.** All 1000 use the *same image*. One image, one deployment. |
| **Isolation** — one bad camera must not kill 999 | **Not this way.** That is *process* isolation. Namespaces add nothing when all 1000 share one trust level, one image, one network need. |
| **Resource limits** — cgroup caps per unit | **Partly.** Useful per shard; overkill per camera. |
| **Lifecycle** — start, stop, restart, place | **No — and this is the failure.** |

### The argument that ends the discussion

An operator adding a camera is *a row in a database*. Making it a container create turns camera CRUD into a deployment operation, which means:

- an orchestrator round-trip sits in the path of a UI click
- the orchestrator's state becomes a mirror of the database's state, and now two systems have to be reconciled
- **camera lifecycle stops working when the orchestrator is unreachable**

That last one is disqualifying. A VMS must keep recording, and keep accepting configuration, through a control-plane outage. Container-per-camera makes the control plane load-bearing for the data plane. Nothing else on this page matters as much.

---

## 2. Sharding: one worker, many pipelines

One worker process holding tens of pipelines. 1000 cameras becomes 20–50 containers — still one image, still one Quadlet unit template.

The reason it wins is that the expensive part is per-*process*, not per-camera: the GStreamer plugin registry, the GLib type system, the thread pools, and — if the worker is written in a managed language — the runtime itself.

| Model | Cost | 1000 cameras |
|---|---|---|
| Container per camera | (baseline + pipeline) × 1000 | **~38 GB** |
| 50-camera shards | (baseline + 50 × pipeline) × 20 | **~9 GB** |

**These are estimates, not measurements** — flagged as such deliberately, because the module can do better. [`shard-memory-probe.py`](./reference/shard-memory-probe.py) measures the two quantities that decide it:

```
B = process baseline   : GStreamer initialised, zero pipelines
I = marginal increment : the cost of pipeline N+1 in a live process

shared: B + N·I        split: N·(B + I)        saving: (N−1)·B
```

It reports **PSS, not RSS**. Summing RSS across N processes counts every page of libgstreamer N times and inflates the split case; PSS divides shared pages by the number of processes mapping them. Getting this wrong is how the naive measurement reaches the naive conclusion — and it is a good half-hour of М11 Lesson 1.

### Sizing the shard

Not by memory — by **restart time and blast radius**. A shard restart costs process start + registry load + N parallel connects. Fifty cameras restart in a few seconds; five hundred do not, and an operator experiences that as "the server hiccuped".

Availability arithmetic says almost any shard size is fine. Perception says 20–100. Independent guidance from the DeepStream community lands in the same place for a much heavier workload: *"10 streams in one pipeline per container is preferred"*, with memory overhead described as low when everything runs in a single process. Recording is far lighter than inference, so the number is higher — but the shape of the advice is identical.

---

## 3. Recording is a branch, not a process

The recording pipeline does not decode:

```
rtspsrc ! rtph264depay ! h264parse ! splitmuxsink
```

Demux and remux. No pixels are ever touched, which is why recording is the cheapest thing in the system and why "another 1000 containers for recording" is answering a question nobody asked.

Split ingest and recording into separate processes and you must either copy every frame across a process boundary or build shared memory. But the real cost is worse than either:

> **Each process opens its own RTSP session.** Cameras commonly cap concurrent sessions at 2–4. Splitting doubles camera-side load and eats the headroom you needed for live viewing.

**The RTSP connection is the resource that must be owned exactly once.** One pipeline holds it; recording, live output and analytics are `tee` branches off the parsed elementary stream. That is one connection, one process, one place where the stream's timestamps are interpreted — which matters more than it sounds, because М8 already established that `ProducerTimestamp` discipline is what makes the timeline honest.

---

## 4. What does deserve separate scaling

Decode and encode. Live transcoding for an operator's 16-up wall, and analytics inference, are a different shape of problem:

| Tier | Sized by | Decodes? | At 1000 cameras |
|---|---|---|---|
| Ingest + record | Camera count | No | 20–50 sharded workers |
| Transcode / analytics | Concurrent demand | Yes | 4–16 workers, GPU-pinned |
| Control plane | Fixed | No | 1 controller |

The middle tier is **demand-driven, not camera-driven** — sized by concurrent viewers and enabled detectors. (This tier becomes М12's *live gateway* — see *Who serves browsers* in [М12's design record](../М12_DomainVMS/module-design.md) — and the rule that a recorder never serves a browser is stated there.) It is also hardware-bound, which is where Nomad's `exec2` and `virt` drivers earn the argument made in [`kubernetes-vs-nomad.md`](../М11_ClusterVMS/kubernetes-vs-nomad.md): a native process needing direct device access does not have to be containerised.

---

## 5. The worker: two supervisors, not one

The instinct to put a supervisor in front of the camera processes is right. The trap is building *one* of them.

| | Owns | Unit of work | Changes when |
|---|---|---|---|
| **Orchestrator** (Nomad) | Worker instances | "a recorder with capacity for 50 cameras" | Capacity changes |
| **Domain controller** | Cameras | "camera 812 should be recording" | An operator changes configuration |

> **The rule: the orchestrator manages shards; the controller manages cameras.**

Adding a camera never touches Nomad. Adding *capacity* does — scale the job from 20 workers to 25. The orchestrator's state stays small, uniform and independent of the customer's camera count, which is what lets the same design serve an 8-camera site and an 8000-camera one.

The controller is a reconciliation loop: desired state in Postgres, actual state reported by workers, and the controller closing the gap by assigning cameras to shards. **М11 sharpens this**: because a recorder is a scheduler allocation with stable identity, a camera is assigned to a recorder *once* and moves with it, so the controller places new cameras and rebalances on request rather than reassigning continuously. Inside each worker a thin supervisor owns its pipelines, watches each one's `GstBus`, and restarts them individually with per-camera backoff.

### Why not extend the process supervisor to cover cameras

Because a process supervisor reconciles *services in dependency order* and a camera placement reconciles *thousands of runtime objects created by operators*. They look similar — both compare desired to actual and both carry a revision — and that resemblance is exactly the trap. A system that grows a second reconciliation loop without deciding which one is authoritative ends up unable to answer "has this configuration change actually taken effect?" with anything better than reasoning across two mechanisms that were never designed to agree.

Keep them layered, not merged: the supervisor knows about workers, the controller knows about cameras, and only the controller knows which camera lives on which worker.

### The failure modes the controller exists to handle

These are what actually take VMS servers down in the field, and none of them are solved by containers:

- **Camera offline** → exponential backoff. Two hundred offline cameras each retrying every second is the single most common cause of a server falling over. Backoff is load-bearing, not an optimisation.
- **Stream stalls while TCP stays open** → watchdog on last-buffer timestamp, *not* on connection state. Endemic to cheap cameras.
- **Two workers claiming one camera** after a reassignment → a fencing token that increments on every ownership change, and a rule that a worker holding a stale token must not write. For archive writers the correct response to a lost claim is to open a *new* segment, never to resume the old one.
- **Disk full** → recording degrades by retention priority. It does not crash.

---

## 6. Where container-per-camera is genuinely right

Stated plainly, because the module teaches it first and should not teach it as a mistake:

- **Under ~50 cameras.** Simpler, legible, and the overhead is irrelevant. This is Lesson 4's world and it is correct there.
- **Multi-tenant sites** where cameras belong to different trust domains — then namespace isolation is doing real work.
- **Heterogeneous pipelines** where a handful of cameras need plugins or drivers the others do not.

The break-even is somewhere near 50, and it is worth having students find it rather than being told.

---

## What this means

| Criterion | Favours |
|---|---|
| Teaching the concepts clearly | **Container per camera** — one unit, one lifecycle, visible in `podman ps` |
| Memory and process count at scale | **Sharded workers** — the baseline is paid once, not 1000 times |
| Surviving a control-plane outage | **Sharded workers** — camera CRUD never reaches the orchestrator |
| Blast radius of a decoder crash | **Container per camera**, marginally — and the gap closes fast with per-pipeline restart |
| Camera-side session limits | **One pipeline owning the connection**, whichever model |
| Operational legibility at 1000 cameras | **Sharded workers** — 25 units instead of 1000 |

**Course decision: teach both, in that order.** Lesson 4 builds container-per-camera on a handful of cameras, because it is the legible thing and it is correct at that scale. М11 Lesson 1 should then break it on purpose — run the probe, show the curve, and derive the shard. That is a better lesson than asserting the conclusion, and it follows the same pattern as the other two records in this folder: the legible choice is right for teaching and wrong for the product.

**Product recommendation: shard, and split the supervisors.** The sharding policy — how many objects per worker, and on what signal to rebalance — is a real design decision with operational consequences, and it does not belong in the schema that records its *outcome*. It needs an owner and a measurement, not a default.

---

## Sources

- [Large number of RTSP streams: nvstreammux or separate pipeline per camera?](https://forums.developer.nvidia.com/t/large-number-of-rtsp-streams-nvstreammux-or-separate-pipeline-per-camera/229269) — "10 streams in one pipeline per container is preferred"; single-process memory overhead described as low
- [Building a multi-camera media server for AI processing on Jetson](https://developer.nvidia.com/blog/building-multi-camera-media-server-ai-processing-jetson) — multi-camera media-server structure
- [Nomad task drivers](https://developer.hashicorp.com/nomad/plugins/drivers) — `exec2` and `virt` for the non-containerised tier
- [`М9_EdgeVMS/reference/shard-memory-probe.py`](./reference/shard-memory-probe.py) — the measurement this record declines to guess at
- Memory figures in §2 are engineering estimates, explicitly not measurements. The probe exists to replace them.

*Written 4 September 2026.*
