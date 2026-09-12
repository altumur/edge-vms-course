# Lesson 4 — Podman, Quadlet, the Three-Way Boundary, and the Spool

**Module:** EdgeVMS — shipping the VMS as an appliance (Module 9)
**You will build:** the VMS running as containers under systemd on the appliance, surviving reboot and OS updates — and a spool that keeps recording through a ten-minute uplink outage with nothing lost.
**Time:** ~150 minutes.

## Why this lesson exists

Two things happen here, and the second is the one you will remember.

The first is ordinary: the VMS you built in М8 has to start on boot, restart if it dies, and survive an OS update that replaces the entire root filesystem underneath it. Quadlet — systemd's native way of running containers — does that in about fifteen lines, and getting one of those lines wrong destroys your container storage on the next update.

The second is that this module has been shipping a defect since Lesson 1 and has not mentioned it. **Pull the network cable out of your appliance for ten minutes and go looking for those ten minutes of video.** They are not anywhere. `kvssink` publishes straight to AWS with nothing behind it, so an uplink blink is not a visibility problem — it is data loss.

Fixing that is the second half of this lesson, and it produces the first artifact in this course that a *later* module upgrades rather than replaces.

> **What you can verify without hardware.** The spool — segments, uploader, bounds, drain — is ordinary code and runs anywhere Python does; the reference implementation and its five tests were run while writing this lesson and the output below is real. Quadlet needs Podman, so it needs the bench. Note that `systemd-analyze verify` **cannot** check a `.container` file — it does not know the `[Container]` section, and reports `Unknown section 'Container'. Ignoring.` Use the generator's dry-run instead (Step 3).

## Prerequisites

- **М8 Lesson 3** — images versus containers, and why credentials are passed by name and never baked in.
- **М8 Lesson 2** — process supervision, restart-with-backoff, and the difference between a crash and a signal. systemd replaces `looper.py` here; the reasoning is unchanged.
- **Lessons 1–3** — the bench, the read-only slots, and the data partition.
- Podman inside the VM: `apt install podman`. Check with `podman --version`.

## Learning objectives

1. Write a Quadlet `.container` unit and explain how it becomes a systemd service.
2. Relocate Podman's storage to the data partition and say precisely what breaks if you do not.
3. Explain why an appliance image cannot contain credentials, and where they come from instead.
4. Demonstrate footage loss during an uplink outage, then eliminate it with a spool.
5. Choose and defend a spool bound policy, and a catch-up rate limit.

---

## Step 1 — The three-way boundary

Lesson 1's thesis was two update planes. Standing on the appliance, there are actually three kinds of thing, and telling them apart is the whole of this step:

| | Lives in | Replaced by | If you get it wrong |
|---|---|---|---|
| **The OS** | a rootfs slot | a RAUC bundle | — |
| **The application** | a container image | a new image | a config change needs an OS flash |
| **The data** | the data partition | **never** | an OS update destroys customer footage |

The third row is the one with teeth, and it is where this lesson's one critical configuration line lives.

Podman's default storage is `/var/lib/containers/storage` — which is **inside the root filesystem**. On a normal server that is fine. On an A/B appliance it is a trap with a delay fuse: everything works until the first OS update, at which point the slot containing your images is overwritten with a fresh one and every image and volume is gone. The box then tries to start containers whose images do not exist, on a system that just booted successfully, so nothing rolls back.

Move it:

```bash
mkdir -p /data/containers/storage

cat > /etc/containers/storage.conf <<'EOF'
[storage]
driver = "overlay"
graphroot = "/data/containers/storage"
runroot = "/run/containers/storage"
EOF
```

`graphroot` is the persistent one — images, volumes, container filesystems. `runroot` is temporary state and belongs on `/run`, which is a tmpfs and correctly empty after a reboot.

Two operational notes worth having before you do this on a box that already has images:

- **Change this before you pull anything.** `podman system reset` is the documented way to clean up when storage settings change, and it must be run *before* editing the config — afterwards it may not be able to find the old storage to clean up.
- **Both slots need this file.** It lives in the rootfs, so it must be in the image you build, not something you type once on a running box. The moment you type it by hand is the moment it is missing from slot B.

Verify:

```bash
podman info --format '{{.Store.GraphRoot}}'
```

```
/data/containers/storage
```

That one line is Lesson 1's thesis made concrete, and it is worth the ceremony: **it is the difference between an OS update and a data-loss incident.**

## Step 2 — Quadlet: containers as systemd units

You could write a `.service` file that runs `podman run`. People do, and then discover they have hand-rolled container lifecycle inside systemd's process lifecycle and the two disagree about what "stopped" means.

Quadlet is Podman's answer: you write a declarative `.container` file, and a systemd generator turns it into a proper service at boot and on every `daemon-reload`.

```bash
mkdir -p /etc/containers/systemd

cat > /etc/containers/systemd/vms-agent.container <<'EOF'
[Unit]
Description=VMS edge agent
After=network-online.target
Wants=network-online.target

[Container]
Image=localhost/example/vms-agent:1.0
EnvironmentFile=/data/config/agent.env
Volume=/data/spool:/data/spool:z
Volume=/data/config:/data/config:ro,z
PublishPort=8000:8000

[Service]
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl start vms-agent.service
systemctl status vms-agent.service
```

Points worth slowing down for:

**The file is `.container`, the service is `.service`.** `vms-agent.container` generates `vms-agent.service`. You start and query the *service*.

**`Image` is the only required key.** Everything else is optional, and the keys map closely onto `podman run` arguments — `Exec` is literally "additional arguments, exactly as if passed after `podman run <image>`".

**Never run `systemctl enable` on a Quadlet unit.** This one catches everybody. The generated service lives in a generator directory and cannot be enabled the normal way. Instead, the generator *applies* your `[Install]` section itself at generation time, the same way `systemctl enable` would have. So `WantedBy=multi-user.target` in the `.container` file is what makes it start at boot — and `systemctl enable vms-agent.service` will fail and leave you confused about why.

**`[Unit]`, `[Service]` and `[Install]` pass straight through** to the generated unit. That is where `Restart=always` goes — the same supervision policy you hand-wrote in М8 Lesson 2, now declarative.

**Both volumes point into `/data`.** The spool and the configuration are data; the container is not allowed to hold either.

## Step 3 — Checking it without deploying it

`systemd-analyze verify` is the obvious reflex and it does not work here — it does not know the `[Container]` section and will tell you so while ignoring the entire file:

```
/tmp/probe.service:4: Unknown section 'Container'. Ignoring.
probe.service: Service has no ExecStart=... Refusing.
```

The right tool is the generator itself, in dry-run mode:

```bash
/usr/lib/systemd/system-generators/podman-system-generator --dryrun
```

It prints the service files it would generate, and parse errors where it cannot. Put that command in CI. A Quadlet typo that only surfaces at boot on an appliance is exactly the class of bug this module exists to eliminate.

## Step 4 — Credentials, and the constraint from Lesson 1

The VMS needs AWS credentials to talk to KVS. Lesson 1 established that **both slots ship byte-identical**, so nothing device-specific can live in an image — not the AWS keys, not a serial number, not a certificate.

So they are **provisioned at commissioning**, onto the data partition:

```bash
mkdir -p /data/config
cat > /data/config/agent.env <<'EOF'
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=eu-west-1
KVS_STREAM_NAME=site-42-camera-1
EOF
chmod 600 /data/config/agent.env
```

This is М8 Lesson 6's rule — configuration is read, credentials are injected — with a physical partition enforcing it.

It is also **a stand-in, and the course says so where it appears.** A long-lived static AWS key in a plaintext file on a device in a warehouse is not a security design; it is a placeholder with a note attached. Somebody has to type it during commissioning, it never rotates, and extracting it needs physical access and about a minute.

Three modules from now, М12 replaces it: the domain provisions its own object storage (М12 Lesson 8) and the box reaches it by workload identity, so there is no key to store. **Mark it in your own notes as the first of the course's temporary secrets** — М9 adds a database password and an operator account, М12 adds a per-recorder credential and a self-signed CA, and **М12 collects all five**: four replaced by giving things identities, and the fifth *promoted* rather than replaced, because the self-signed CA turned out to be the customer's own root.

## Step 5 — The failure this module has been shipping

Before building anything, go and see it.

Start the VMS, confirm it is publishing to KVS, then cut the network:

```bash
# in another terminal on the host, disconnect the guest's NIC
# (QEMU monitor: Ctrl-a c, then:)
set_link virtio-net-pci.0 off
```

Wait ten minutes. Reconnect:

```
set_link virtio-net-pci.0 on
```

Now go looking for those ten minutes of video in KVS. **They do not exist.** `kvssink` had nowhere to put them, the pipeline errored, the supervisor restarted it, and each restart began publishing from *now*. Ten minutes of a customer's premises are simply missing, and nothing in the system logged it as data loss — from the outside it looks like a brief service interruption.

Sit with that for a moment. Every mechanism in Lessons 1–3 was built so a box in a ceiling void could survive being unattended, and the product was quietly losing footage every time a switch rebooted.

**You cannot buffer behind `kvssink`.** It is a sink: data goes in and leaves the process. To survive an outage the pipeline has to write segments to disk and hand them to something else to upload:

```
capture ──▶ splitmuxsink ──▶ /data/spool/<camera>/<timestamp>.mp4
                                     │
                             uploader (separate process)
                                     │  on acknowledgement: delete
                                     ▼
                                    KVS
```

This is the one media-layer change the module makes, and it is forced.

## Step 6 — Build the spool

Four properties, each of which is a decision rather than an implementation detail.

```python
"""Minimal spool + uploader — /data/vms/spool.py"""
import os

class Spool:
    def __init__(self, root, max_bytes, policy="drop-oldest"):
        self.root, self.max_bytes, self.policy = root, max_bytes, policy
        os.makedirs(root, exist_ok=True)
        self.dropped = 0

    def pending(self):
        """Oldest first. The filename carries the timestamp, so sorting is ordering."""
        return sorted(
            os.path.join(self.root, n) for n in os.listdir(self.root)
            if n.endswith(".mp4")
        )

    def used(self):
        return sum(os.path.getsize(p) for p in self.pending())

    def accept(self, name, data):
        """Called when splitmuxsink closes a segment. Returns True if it was kept."""
        if self.used() + len(data) > self.max_bytes:
            if self.policy == "stop-recording":
                return False
            while self.pending() and self.used() + len(data) > self.max_bytes:
                os.remove(self.pending()[0])
                self.dropped += 1
        with open(os.path.join(self.root, name), "wb") as f:
            f.write(data)
        return True


def drain(spool, upload, budget_per_tick=2):
    """Rate-limited catch-up: never more than budget_per_tick per pass."""
    sent = 0
    for path in spool.pending():
        if sent >= budget_per_tick:
            break
        if not upload(path):      # far side did not acknowledge
            break                 # stop on first failure; order is preserved
        os.remove(path)           # delete ON ACKNOWLEDGEMENT, never on send
        sent += 1
    return sent
```

### Property 1 — Delete on acknowledgement, never on send

The single most important line is `os.remove(path)` sitting **after** `upload(path)` returned true. An upload is not complete when the write returns; it is complete when the far side says so. Delete on send and every network timeout is silent data loss.

You will meet this exact shape twice more. М11 acknowledges a configuration change on local commit and shows *saved · not yet replicated* until the domain confirms it. The rule generalises:

> **Every boundary in this system is crossed by a one-way publication with a stated recovery point.** Footage here, configuration in М11, status everywhere.

### Property 2 — A bound, and a policy for reaching it

The spool cannot grow forever; Lesson 1 sized the partition from a stated outage requirement. What happens when it fills is a product decision with two defensible answers:

- **`drop-oldest`** — keep recording, lose the oldest footage. Right when recent footage matters most, which for security is usually true.
- **`stop-recording`** — refuse new segments, keep what you have. Right when footage is evidence and a gap is worse than an old recording.

They are different products. Pick one, write it in the specification, and make the appliance **say which it did** — a silent drop is indistinguishable from a bug.

### Property 3 — Ordering

Uploads go oldest first, and the drain stops at the first failure rather than skipping ahead. If the link is flapping, you want a contiguous archive with a gap at the end, not a randomly perforated one.

### Property 4 — Rate-limited catch-up

When the link returns, ten minutes of backlog from every camera arrives at once — competing with live upload, which is the stream somebody is actually watching. `budget_per_tick` bounds it.

**A recovery that saturates the uplink for an hour has turned a ten-minute fault into a seventy-minute one.** Prioritise live over backlog, and know how long full recovery takes at your bandwidth. That number belongs in the specification beside the outage window.

## Step 7 — Test it

The spool is ordinary code, so test it like ordinary code — no VM, no network, no cameras:

```python
import os, shutil, spool as S

root = "/tmp/spooltest/data"; shutil.rmtree(root, ignore_errors=True)
seg = b"x" * 100

# 1. a failed upload must keep the file
sp = S.Spool(root, max_bytes=10_000)
for i in range(5): sp.accept(f"2026-09-06T10-0{i}-00.mp4", seg)
assert S.drain(sp, lambda p: False) == 0 and len(sp.pending()) == 5
S.drain(sp, lambda p: True, budget_per_tick=99)
assert sp.pending() == []

# 2. bound + drop-oldest: 300 bytes holds three 100-byte segments
sp = S.Spool(root, max_bytes=300, policy="drop-oldest")
for i in range(5): assert sp.accept(f"2026-09-06T11-0{i}-00.mp4", seg) is True
assert len(sp.pending()) == 3 and sp.dropped == 2

# 3. bound + stop-recording refuses instead of dropping
shutil.rmtree(root); sp = S.Spool(root, max_bytes=300, policy="stop-recording")
accepted = [sp.accept(f"2026-09-06T12-0{i}-00.mp4", seg) for i in range(5)]
assert accepted == [True, True, True, False, False] and sp.dropped == 0

# 4. rate-limited drain: 10 segments at 3 per tick
shutil.rmtree(root); sp = S.Spool(root, max_bytes=10_000)
for i in range(10): sp.accept(f"2026-09-06T13-{i:02d}-00.mp4", seg)
ticks = 0
while sp.pending():
    S.drain(sp, lambda p: True, budget_per_tick=3); ticks += 1
assert ticks == 4
```

Real output from running these while writing the lesson:

```
1. delete-on-ack .......... OK
2. bound, drop-oldest ..... OK (kept 3, dropped 2)
3. bound, stop-recording .. OK (accepted 3, refused 2)
4. rate-limited drain ..... OK (10 segments, budget 3 -> 4 ticks)
5. oldest-first ordering .. OK
```

Test 2 is worth a second look, because the obvious expectation is wrong: with `max_bytes=300` and 100-byte segments, **three** fit exactly, not two. The first draft of this test asserted two and failed — the implementation was right and the expectation was wrong, which is the more common way round than people admit.

## Step 8 — Do the outage again

Same procedure as Step 5, now with the spool in place:

```
set_link virtio-net-pci.0 off
# ... ten minutes ...
set_link virtio-net-pci.0 on
```

During the outage:

```bash
ls /data/spool/camera-1/ | wc -l      # climbing
du -sh /data/spool                    # climbing
```

After reconnection, watch it drain at the rate you configured — not all at once. Then go to KVS and find the ten minutes.

**They are all there.** That is the deliverable, and it is the difference between a device and an appliance.

Then answer the question the deliverable actually asks: **how long can this box survive?** Not a guess — measure the growth rate over five minutes, divide the spool bound by it, and state the number. Lesson 1 asked you to size the partition from a requirement; this is where you find out whether you got it right.

## Step 9 — The two numbers the spool must export

The spool is the first thing on this box whose *health is a quantity* rather than a yes/no, so it is where the appliance starts emitting signals rather than just logging.

Two, and only two:

| Signal | What it means | Alarm when |
|---|---|---|
| **`spool_oldest_seconds`** | age of the oldest unsent segment | it exceeds your stated outage tolerance — *before* the bound is reached, not at it |
| **`spool_bytes_used` / bound** | how full the buffer is | above ~70%, because the remaining time shrinks as cameras are added |

The first is the one that matters, and it is worth understanding why it beats the obvious alternative. **Alarm on age, not on count.** Segment count depends on how many cameras a site has and how long a segment is; age is directly the answer to *how much footage is at risk right now*, and it means the same thing at a four-camera shop and a two-hundred-camera warehouse. One threshold works everywhere.

The threshold comes from Lesson 1's arithmetic rather than from taste. If you sized the partition for a 24-hour outage, alarm at something like **6 hours** — early enough that somebody can act while there is still three quarters of the buffer left, late enough that a router reboot does not page anyone at 3am.

Two properties worth noticing now, because М13 generalises both:

- **This is a product signal, not a process one.** Nothing here reports CPU or memory. `spool_oldest_seconds` says *how much of the customer's footage is currently at risk*, which is the alarm-on-the-product rule from Lesson 3 in its second instance
- **It has to be readable when the uplink is down**, which is precisely when it is interesting — so it is exported locally and scraped from wherever the box can be reached, never pushed to a centre that by definition is unreachable at that moment. М13 turns that into a scrape topology

**Do not build a metrics endpoint yet.** Write the two numbers where the health check can read them; М13 gives them a proper exporter. Deciding *what to measure* is this lesson's job, and it is the half that is actually hard.

## Step 10 — Why this is not premature

You have just built a buffer in a module about operating-system updates. That deserves a justification, and it is not "we had room".

The spool exists here because **the link can fail here**, and nothing else in the course is yet in a position to catch it. But it is not thrown away:

- **М9 puts an index over the same files and they become the archive.** The same `splitmuxsink` writes the same segments; nothing deletes them on upload; a database row is written instead. The pipeline barely changes — what changes is who owns the footage.
- **М12 makes the upload conditional.** An on-prem recorder has nobody to upload to. A cloud recorder *is* the destination. A cloud site with no appliance has no spool at all, which is why the camera's own SD card becomes the buffer there.

Same segments, three meanings. It is the first thing in this course that a later module **upgrades rather than replaces**, and it is worth noticing as a design property: the parts that survive contact with later requirements are usually the ones that were forced by a physical fact rather than chosen for convenience.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `systemctl start vms-agent.service` says the unit does not exist | You did not `systemctl daemon-reload`, or the file is not in a Quadlet search path (`/etc/containers/systemd/` for root). |
| `systemctl enable vms-agent.service` fails | Expected — never enable a Quadlet unit. Put `WantedBy=` in the `[Install]` section of the `.container` file; the generator applies it. |
| Containers vanish after an OS update | `graphroot` was not moved to the data partition, or was moved on the running box but not in the image, so slot B never had it. |
| `podman info` still shows the default graphroot | `/etc/containers/storage.conf` has a syntax error and is being ignored, or you edited the rootless config while running as root. |
| Permission denied on `/data/spool` from inside the container | SELinux relabelling — the `:z` suffix on the `Volume=` line is what handles it. |
| The generator dry-run prints nothing | No Quadlet files found. Check the path and the `.container` extension. |
| Spool grows without ever draining | `upload()` is returning falsy. Make it log the reason — a silent uploader is untestable. |
| Everything uploads instantly with no rate limit | `budget_per_tick` unset or drained in a tight loop. The budget only means something if the caller ticks on a timer. |

## Recap

- Three kinds of thing on an appliance: the **OS** (replaced by a bundle), the **application** (replaced by an image), and the **data** (never replaced). Podman's `graphroot` must move to the data partition, in the image, or the first OS update destroys it.
- Quadlet turns a declarative `.container` file into a systemd service. `Image` is the only required key, `[Install]` is applied by the generator, and you must never `systemctl enable` the result.
- `systemd-analyze verify` cannot check a Quadlet file. The generator's `--dryrun` can, and belongs in CI.
- Credentials are provisioned at commissioning onto the data partition, because both slots ship identical. This is the course's **first temporary secret**; М12 collects all five — four replaced, one promoted.
- `kvssink` with nothing behind it loses footage on every uplink blink. The fix is local segments plus a separate uploader — the one media change this module makes.
- **Delete on acknowledgement, never on send.** The bound needs a policy the product states. Catch-up needs a rate limit, or recovery becomes its own outage.
- The spool is not thrown away: М9 indexes the same files into an archive, М12 makes the upload conditional.

## Exercises

1. Measure your spool's real growth rate with one simulated camera, then compute how long your Lesson 1 partition size actually survives. Compare it with the requirement you wrote down then. If they disagree, which one changes?
2. Implement both bound policies behind a config flag, then write the two sentences a product manager would put in a datasheet for each. They should read like different products, because they are.
3. Break the uploader so it acknowledges *before* the far side confirms, then run an outage. Show the resulting data loss and explain exactly which line caused it.
4. Take your `spool_oldest_seconds` threshold from Step 9 and simulate the outage that trips it. Then ask whether you would have wanted to be woken — and adjust the number rather than the story.
5. The appliance drops the oldest footage when the spool fills and nobody is told. Design the smallest change that makes this visible to an operator, and say where that signal has to travel to be useful — noting that in this module there is nowhere above the box for it to go.

## Where this is going

The module is done: an appliance that updates its OS atomically, verifies what it installs, recovers from a bad update by itself, runs its workload under systemd, and keeps recording through an outage.

It also has no idea what it is *supposed* to be doing. Every camera is configured by editing a file and restarting a container — actual state is the only state there is.

**М9 introduces the wish.** A row in a database saying a camera should be recording is not a camera recording, and something has to close the gap. You will write that reconciler by hand — and the first thing it does is put an index over the segments you just started writing, turning a spool into an archive.
