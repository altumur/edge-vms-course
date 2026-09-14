# Lesson 7 — Fifty Pipelines in One Process

**Module:** EdgeVMS — the box owns its truth (Module 9)
**You will build:** the real actuator — fifty GStreamer pipelines in one Python process, stall detection that never touches a buffer, and the moment М9's spool becomes an archive.
**Time:** ~150 minutes.

## Why this lesson exists

This is the module's technical centre, and it exists to settle an argument you will have with yourself and later with a colleague: **fifty video pipelines in one Python process sounds impossible, and it is not.** It is also one specific mistake away from being impossible, and the point of the lesson is that you can see the boundary rather than fearing the whole territory.

`INSERT INTO cameras` starts working here.

> **What you can verify without hardware — read this before starting.** Unlike Lessons 1 and 2, most of this lesson needs a real GStreamer. The design reasoning below is sourced from PyGObject's and GStreamer's own documentation and is quoted directly. **The numbers are not asserted — you produce them**, with the probe script that ships with this module. Where this text gives a figure it is an order of magnitude to check your result against, not a claim about your hardware.

## Prerequisites

- **Lesson 6** — the reconcile loop and its state vocabulary. You are replacing one function.
- **М8 Lesson 4** — GStreamer pipelines, elements, pads and caps. Built there with `gst-launch-1.0`; built here from Python.
- **М9 Lesson 4** — the spool, `splitmuxsink`, and delete-on-acknowledgement. Step 7 is where that changes.
- PyGObject and GStreamer with the bad plugins:
  - **Debian/Ubuntu:** `sudo apt install python3-gi gstreamer1.0-plugins-{base,good,bad} gir1.2-gst-plugins-base-1.0`
  - Verify: `python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst; Gst.init(None); print(Gst.version_string())"`
  - And: `gst-inspect-1.0 watchdog` must print an element, not an error.

## Learning objectives

1. Build and control a GStreamer pipeline from Python rather than a shell string.
2. Explain where the work actually happens, and why the GIL is mostly uninvolved.
3. Demonstrate the one seam that kills a Python media worker, by crossing it deliberately.
4. Detect a stalled stream without touching a buffer.
5. Drain many pipelines' buses from asyncio without a second event loop.
6. Convert the spool into an archive by changing what happens after a segment closes.

---

## Step 1 — A pipeline from Python

```python
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
Gst.init(None)

DESC = (
    "rtspsrc location={url} protocols=tcp latency=200 name=src ! "
    "rtph264depay ! h264parse ! "
    "watchdog timeout=8000 ! "
    "splitmuxsink location={out}/%%05d.mp4 max-size-time=600000000000"
)

url = compose_rtsp_url(cam)          # decrypts cred_secret, in memory only
pipeline = Gst.parse_launch(DESC.format(url=url, out=cam["dir"]))
pipeline.set_state(Gst.State.PLAYING)
```

`Gst.parse_launch` takes the same string syntax as `gst-launch-1.0` from М8 Lesson 4, so everything you learned there transfers. Building the graph element-by-element with `Gst.ElementFactory.make` is the alternative; it is more code and buys nothing until you need to reach into a pipeline at runtime.

**`compose_rtsp_url` is where Lesson 5's split gets paid for.** The credential is decrypted, used, and never stored back into a variable that outlives the call — because the composed URL is about to be interpolated into a pipeline description that GStreamer will happily print in an error message. Log `cam["rtsp_url"]`, never `url`, and put that in a comment so the next person does not "simplify" it.

Two properties are doing real work:

- **`protocols=tcp`.** RTSP over UDP loses packets and gives you corrupt recorded segments — not slightly worse pictures, actually broken files. Over anything but a quiet LAN, use TCP. This matters even more in М12, where the camera may be streaming across the internet.
- **`max-size-time=600000000000`** — ten minutes in nanoseconds. Segment length is a real product decision: it bounds what a crash loses (Lesson 8), it sets the granularity of retention, and it decides how many index rows you write.

## Step 2 — Where the work happens, and why fifty is fine

Once `set_state(PLAYING)` returns, **buffers move on GStreamer's own native threads, inside libgstreamer, in C.** Python is not in that path.

PyGObject documents the consequence directly: *"all PyGObject calls release the GIL during their execution and other Python threads can be executed during that time."*

So a worker holding fifty recording pipelines is running fifty pipelines' worth of C and a trickle of Python:

| Python does | How often |
|---|---|
| Read a bus message | every few seconds per pipeline |
| Change a pipeline's state | when configuration changes |
| Name a segment file | once per segment — every ten minutes |
| Write status | every five seconds, for the whole worker |

Add that up across fifty cameras and it is a few hundred Python calls per minute. The GIL is close to uninvolved, and the intuition that "Python can't do video" is answering a question nobody asked: nothing here is *decoding* in Python, or touching pixels in Python, or even seeing a buffer in Python.

## Step 3 — The seam, crossed deliberately

Now break it, because a boundary you have crossed is a boundary you remember.

PyGObject also documents: *"signals get executed in the context they are emitted from."* A callback attached to a signal or a pad probe therefore runs **in the streaming thread**, and to run Python there it must take the GIL.

Add a buffer probe — the innocent-looking way to timestamp arrivals so you can detect a stall:

```python
def on_buffer(pad, info):
    cam["last_buffer"] = time.monotonic()      # looks harmless
    return Gst.PadProbeReturn.OK

pad = pipeline.get_by_name("src").get_static_pad("src")
pad.add_probe(Gst.PadProbeType.BUFFER, on_buffer)
```

Run it on one camera. Fine. Run it on fifty at 25 fps and do the arithmetic:

```
50 cameras × 25 fps = 1,250 GIL acquisitions per second
```

Twelve hundred and fifty times a second, a C thread stops, takes a single global lock, runs an interpreter, and releases it — all to store a float. Watch the worker's CPU climb, watch buses stop being drained on time, watch pipelines start reporting late.

**Measure it, then delete the probe and measure again.** The two numbers are the lesson. Then write the rule down:

> **Python touches control, never data.**
>
> **Banned in the recording path:** `appsink`, `identity handoff`, buffer-level pad probes.
> **Fine:** bus messages, state changes, `splitmuxsink::format-location` (once per segment).

### The rule is not really about the GIL

Worth stating now, because it is the part that survives a change of language. The constraint is not Python's lock. It is **crossing a language boundary once per frame**:

| | What a per-buffer callback costs | Verdict |
|---|---|---|
| **Python** | Acquire the GIL and interpret. 1,250 acquisitions/second through one lock | Fatal |
| **Go** | Enter the Go runtime from a C thread through cgo. Cheaper, still real, and the rules on passing pointers make it awkward | Same discipline required |
| **C++** | Nothing. There is no boundary | The rule dissolves |

That last row is the real argument for C++ in the media worker — a better one than "C++ is faster", which for a pipeline that never decodes would barely be true. Lesson 9 comes back to it.

## Step 4 — Stall detection without touching a buffer

Here is the failure the probe was trying to catch, and it is a genuinely nasty one: **a camera stops sending video while its TCP socket stays open.** Nothing errors. `rtspsrc` sits there contentedly. The pipeline is PLAYING. Zero bytes arrive, and your archive quietly develops a hole.

Detecting it means noticing that buffers stopped — which is per-buffer work, which is banned.

GStreamer already solved this in C. The `watchdog` element from `gst-plugins-bad` passes buffers through untouched and **posts an error on the bus** if none arrive within `timeout` milliseconds:

```
rtspsrc ! rtph264depay ! h264parse ! watchdog timeout=8000 ! splitmuxsink
```

```bash
gst-inspect-1.0 watchdog
```

The default `timeout` is 1000 ms, which is too tight for cameras — a keyframe interval plus a hiccup will trip it. A few seconds is right; 8000 is a reasonable start and worth tuning against your actual hardware.

Zero Python in the data path, and the failure arrives **on the bus the worker is already reading**. One element, and it is the model for the whole design: push the per-frame concern into C, keep Python at control rate.

## Step 5 — Draining buses without a second event loop

The worker speaks asyncio — to Postgres, to its API. GStreamer's own idiom is a `GLib.MainLoop`. Running both gives your process two schedulers and two notions of "later", and every bug after that is a scheduling bug.

Don't. Each pipeline has its own bus, and one asyncio task drains all of them with the **non-blocking** `pop_filtered` on a short tick:

```python
async def pump_buses(self, tick=0.2):
    while True:
        for cam_id, p in self.pipelines.items():
            bus = p.get_bus()
            while True:
                msg = bus.pop_filtered(
                    Gst.MessageType.ERROR | Gst.MessageType.EOS |
                    Gst.MessageType.STATE_CHANGED | Gst.MessageType.ELEMENT)
                if msg is None:
                    break
                self.handle(cam_id, msg)
        await asyncio.sleep(tick)
```

Fifty non-blocking pops every 200 ms costs nothing measurable — `pop_filtered` with no message returns immediately. The tidier version uses `bus.get_pollfd()` with `loop.add_reader()` so there is no polling at all; it is a good exercise and not worth the fragility as a default.

Note `pop_filtered` and not `timed_pop_filtered`. The timed variant blocks, and blocking inside an asyncio task stops every other task in the process — including reconciliation.

## Step 6 — The per-camera state machine

```
IDLE ──▶ STARTING ──▶ RUNNING ──▶ FAILED ──▶ (backoff) ──▶ STARTING
                          │                       ▲
                          └───────────────────────┘
                              watchdog / ERROR on bus
```

This lives in a `CameraPipeline` object, one per camera, driven by the loop from Lesson 6 — **not** in a coroutine per camera, for the reasons that lesson gave.

The backoff you wrote in Lesson 6 needs no changes. It was tested against a fake actuator that returned `False`; here `False` means `set_state` returned `Gst.StateChangeReturn.FAILURE` or the bus produced an error before `RUNNING`. **That the policy did not have to change is the point of having built it separately** — and it is worth noticing explicitly, because it is the same property that lets Lesson 9 argue the whole thing could be rewritten in Go without redesigning anything.

## Step 7 — The spool becomes the archive

Here is the module's thesis in one diff, and it is smaller than it should be.

М9 Lesson 4 wrote segments to `/data/spool` and an uploader deleted each one after KVS acknowledged it. The pipeline was:

```
rtspsrc ! rtph264depay ! h264parse ! splitmuxsink location=/data/spool/%05d.mp4
```

Here it is:

```
rtspsrc ! rtph264depay ! h264parse ! watchdog ! splitmuxsink location=/data/archive/<cam>/e1/%05d.mp4
```

**The same element writes the same files.** What changed is what happens when a segment closes. In М9, an uploader eventually deleted it. Here, nothing deletes it — you write an index row instead:

```python
def on_segment_closed(self, cam_id, path, start, end):
    self.pending_index.append((cam_id, start, end, path, os.path.getsize(path)))
```

```sql
INSERT INTO segments (camera_id, span, path, bytes, epoch)
VALUES ($1, tstzrange($2, $3, '[)'), $4, $5, 1);
```

That is it. The spool became an archive because **somebody started keeping a record of what was in it.**

Two things follow that are worth saying out loud:

**The uploader becomes optional.** On an on-prem recorder there is nobody to upload to — footage lives where it was recorded and the console reads the index. In М12 a cloud recorder *is* the destination and the upload comes back. Same segments, different meaning, no rewrite.

**`epoch` is 1 and never changes in this module.** It is in the path, and in the index, and it does nothing. It is there because in М11 two instances of the same recorder can briefly exist during failover, and the epoch in the path is what stops the stale one writing over the live one's files. Adding it now costs a directory level; adding it later means moving every file in the archive.

Use `splitmuxsink`'s `format-location` signal to build the path — **once per segment**, which is control rate and therefore allowed.

## Step 8 — Measure it

Re-run М9's probe against the real worker, and extend it to report threads:

```bash
python3 reference/shard-memory-probe.py --pipelines 50
```

Two numbers matter, and they are the ones that decide shard size in М11:

- **`B`, the process baseline** — what the interpreter, PyGObject and GStreamer cost before any camera exists.
- **`I`, the marginal cost per pipeline** — what each additional camera adds.

Report **PSS, not RSS**. RSS counts a shared library page once *per process*, so summing RSS across workers double-counts every page of libgstreamer. PSS divides shared pages by the number of processes mapping them, which is the number you can actually add up.

Thread count too: each recording pipeline creates roughly three to five native threads, so fifty cameras is 150–250 threads in one process. Linux is entirely comfortable with that — but measure it rather than assume, because it is the first number a reviewer will challenge.

### The cost of sharding, stated honestly

**One segfault takes the whole shard.** Fifty cameras stop, not one.

That is the price paid for the per-process baseline, and it is bounded rather than eliminated: by shard size (fifty, not a thousand), by systemd restarting the unit in seconds, and by `splitmuxsink` — a crash loses the open segment and nothing already closed. Lesson 8 measures that loss.

**Deliverable:** `INSERT INTO cameras` produces a recording; `DELETE` stops it. Fifty pipelines in one process with measured PSS and thread count. And a `git diff` against М9's pipeline that fits on one screen.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `gst-inspect-1.0 watchdog` finds nothing | `gst-plugins-bad` is not installed. The element lives there, not in base or good. |
| Corrupt or unplayable recorded segments | RTSP over UDP with packet loss. Set `protocols=tcp`. |
| A camera password appears in a log or a bus error | Something logged the composed URL rather than the credential-free one. This is why Lesson 5 split the column. |
| The worker is fine at 5 cameras and collapses at 50 | A per-buffer callback survived. Search for `add_probe`, `appsink`, and `identity`. Step 3. |
| Bus messages arrive seconds late | `pump_buses` is blocked — usually `timed_pop_filtered` instead of `pop_filtered`, or blocking I/O in a handler. |
| Pipelines go PLAYING then immediately error | Read the actual bus error rather than guessing; `rtspsrc` reports authentication and unreachable-host distinctly. |
| The watchdog fires on healthy cameras | `timeout` too tight for the keyframe interval. Default is 1000 ms; cameras want several seconds. |
| Memory grows steadily over hours | Pipelines set to NULL but never unreffed, or `CameraPipeline` objects kept in a dict after stopping. Watch PSS across a long run. |
| Segments recorded but nothing in the console | You changed the sink but not what happens after it — the index row from Step 7. |

## Recap

- Buffers move on GStreamer's native threads in C. PyGObject **releases the GIL during its calls**, so fifty pipelines cost a few hundred Python calls a minute.
- Signals and pad probes run **in the streaming thread**. One buffer probe across fifty cameras at 25 fps is 1,250 GIL acquisitions per second, and that — not the number of pipelines — is what kills a Python worker.
- **Python touches control, never data.** The rule generalises: it is about crossing a language boundary per frame, so Go needs the same discipline and C++ dissolves it.
- The `watchdog` element detects a stalled-but-open stream in C and reports it on the bus you already read.
- One asyncio task drains every bus with non-blocking `pop_filtered`. No second event loop.
- The backoff policy from Lesson 6 needed **no changes** when the actuator became real. That is the payoff for building the loop first.
- **The spool became an archive because something started keeping a record of it.** Nothing deletes segments now; an index row is written instead. `epoch` is present, unused, and saves an archive-wide migration in М11.
- Measure PSS, not RSS. One segfault takes the whole shard — bounded by shard size, systemd, and segment discipline.

## Exercises

1. Add the buffer probe, measure CPU and bus latency at 10, 30 and 50 cameras, then remove it and repeat. Plot both. This is the most convincing graph in the module.
2. Find `B` and `I` for your hardware, then compute how many cameras fit in 2 GB. Keep the number — М11 Lesson 1 asks for it to size a shard.
3. Unplug a camera's network cable mid-recording and time how long the `watchdog` takes to report. Then set `timeout=1000` and find the false-positive rate on a healthy camera.
4. Replace `pump_buses`'s polling with `bus.get_pollfd()` and `loop.add_reader()`. Measure whether the difference is detectable at 50 cameras. Then decide whether you would ship it.
5. Kill the worker with `SIGKILL` mid-segment and work out exactly what was lost, in seconds of footage and in index rows. Lesson 8 makes this a test; predicting it first is the point.

## Where this is going

You can start and stop cameras from SQL, and fifty of them run in one process. Everything so far has assumed things mostly work.

**Lesson 8 assumes nothing works.** Cameras go offline, streams stall with the socket open, the disk fills, and the worker is killed mid-segment — each induced on purpose, each handled, each asserted by a test. It is also where Lesson 5's partitioning earns its place, because retention has to run *while* the disk is full, and where the fencing rule arrives in its smallest form: on restart, never resume the previous segment.
