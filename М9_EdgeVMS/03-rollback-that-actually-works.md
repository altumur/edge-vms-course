# Lesson 3 — Rollback That Actually Works

**Module:** EdgeVMS — shipping the VMS as an appliance (Module 9)
**You will build:** boot-attempt logic in GRUB, a health check that decides whether an update is kept, and a written record of three deliberately induced failures and the recoveries that followed.
**Time:** ~120 minutes.

## Why this lesson exists

Lesson 2 ended with a signed system installed in the inactive slot and activated for the next boot. Reboot and you are running it.

Now suppose it does not work.

Most people's mental model of rollback is "if it fails, boot the old one" — which sounds complete and is missing the only hard part. **Who notices? And what counts as failure?** A kernel panic is easy. A system that boots perfectly, brings up networking, starts every service, and cannot talk to a single camera is the failure that actually happens, and no bootloader on earth will detect it for you.

This lesson is about making the appliance answer both questions by itself, and then proving it does by breaking it on purpose three times.

> **What you can verify without hardware.** All of it, on the Lesson 1 bench. The GRUB scripting and `grub-editenv` work identically in QEMU and on metal. What a VM cannot show you is a genuine hardware-dependent failure — a driver that works on the bench and not on the real board — and Exercise 5 is about what that means for your test strategy.

## Prerequisites

- **Lesson 2** — a working `rauc install`, your CA, and a bundle you can rebuild.
- **Lesson 1** — the two-slot bench and `/etc/slot-id`.
- **М8 Lesson 2** — process supervision, exit codes, and the difference between a crash and a clean shutdown. The health check in Step 3 is that same reasoning, one level up.

## Learning objectives

1. Explain why "did it boot?" is an inadequate definition of a successful update, with a concrete example.
2. Describe the try/OK state machine RAUC and GRUB implement between them, and say which component owns which half.
3. Read and write GRUB environment variables with `grub-editenv`.
4. Write a health check that decides whether a new system is kept, and defend where you drew the line.
5. Induce three distinct failures and document the observed recovery for each.

---

## Step 1 — The state machine, and who owns which half

RAUC does not decide what boots. **GRUB does.** RAUC reads and writes variables in the GRUB environment; the selection logic lives in `grub.cfg` as shell script. This surprises people and it is worth internalising, because it means the boot logic is yours to write, and yours to get wrong.

Three variables per the reference implementation RAUC ships:

| Variable | Meaning |
|---|---|
| `ORDER` | The slots to try, in priority order — e.g. `"B A"` after an install into B |
| `<slot>_OK` | This slot is **known good**. Something ran and confirmed it |
| `<slot>_TRY` | This slot is **being attempted right now**. Set by GRUB before booting it |

The rule GRUB applies is one line: **boot the first slot in `ORDER` where `OK == 1` and `TRY == 0`.**

Follow it through an update:

```
1. Steady state          A: OK=1 TRY=0     B: OK=1 TRY=0     ORDER="A B"
                         → boots A

2. rauc install → B      A: OK=1 TRY=0     B: OK=0 TRY=0     ORDER="B A"
                         → B is first, but OK=0, so it is skipped... 
```

...which is where the design earns its keep. RAUC marks the newly installed slot as the one to *try*, and GRUB sets `TRY=1` as it hands over. If the new system comes up and proves itself, something inside it sets `OK=1` and clears `TRY`. If it does not — panic, hang, failed health check, anything at all — then on the next boot GRUB finds `B_TRY` still set, skips B, and boots A.

**Nothing had to detect the failure.** The absence of a success signal *is* the failure signal. That inversion is the whole trick, and it is what makes the mechanism robust against failure modes you did not anticipate — including the box losing power mid-boot, which no watchdog inside the box could have reported.

The reference `grub.cfg` RAUC ships allows exactly **one attempt per slot**, because GRUB's scripting is too limited for a real counter. Say that out loud: your appliance gets one shot at a new system. If you want three, you write the counter yourself, and you find out quickly why the RAUC authors did not.

## Step 2 — Put that logic in GRUB

Boot **slot A** and replace the `grub.cfg` from Lesson 1. This is the version with actual logic in it:

```bash
mount -o remount,rw /boot/efi 2>/dev/null || mount /dev/vda1 /boot/efi

cat > /boot/efi/EFI/BOOT/grub.cfg <<'EOF'
set timeout=3

set ORDER="A B"
set A_OK=0
set B_OK=0
set A_TRY=0
set B_TRY=0
load_env --file=(hd0,gpt1)/grubenv

# --- choose a slot -------------------------------------------------
set default=9
for SLOT in $ORDER; do
    if [ "$SLOT" == "A" ]; then
        INDEX=0 ; OK=$A_OK ; TRY=$A_TRY ; A_TRY=1
    fi
    if [ "$SLOT" == "B" ]; then
        INDEX=1 ; OK=$B_OK ; TRY=$B_TRY ; B_TRY=1
    fi
    if [ "$OK" -eq 1 -a "$TRY" -eq 0 ]; then
        set default=$INDEX
        break
    fi
done

# --- nothing bootable: clear the try flags and fall through --------
if [ "$default" -eq 9 ]; then
    if [ "$A_OK" -eq 1 -a "$A_TRY" -eq 1 ]; then set A_TRY=0 ; fi
    if [ "$B_OK" -eq 1 -a "$B_TRY" -eq 1 ]; then set B_TRY=0 ; fi
    set default=0
fi

save_env --file=(hd0,gpt1)/grubenv A_TRY A_OK B_TRY B_OK ORDER

menuentry "Slot A (OK=$A_OK TRY=$A_TRY)" {
    search --no-floppy --label rootfs0 --set root
    linux /vmlinuz root=LABEL=rootfs0 ro console=ttyS0,115200 rauc.slot=A
    initrd /initrd.img
}

menuentry "Slot B (OK=$B_OK TRY=$B_TRY)" {
    search --no-floppy --label rootfs1 --set root
    linux /vmlinuz root=LABEL=rootfs1 ro console=ttyS0,115200 rauc.slot=B
    initrd /initrd.img
}
EOF
```

Two things to notice.

**`load_env --file=` points at the ESP, not the default `/boot/grub/grubenv`.** RAUC's documentation is explicit that this environment must live outside the redundant partitions, because the rootfs slots have to be replaceable without affecting it. Put it inside a slot and an update wipes the record of which slot to boot — a bootstrap problem with no bootstrap.

**The menu entry titles print the variables.** Purely a teaching device, and worth keeping through this lesson: you can read the state machine off the boot menu without logging in, which makes the next three failures far easier to follow.

Create the environment file and seed it:

```bash
grub-editenv /boot/efi/grubenv create
grub-editenv /boot/efi/grubenv set ORDER="A B" A_OK=1 A_TRY=0 B_OK=1 B_TRY=0
grub-editenv /boot/efi/grubenv list
```

```
ORDER=A B
A_OK=1
A_TRY=0
B_OK=1
B_TRY=0
```

That command is your window into the whole mechanism. Run it after every step from here on.

## Step 3 — Decide what "working" means

Here is the part that is engineering judgement rather than configuration, and where most real appliances are weakest.

The new system boots. Something must now decide whether to keep it. **That decision is a health check, and its definition is a product decision you cannot delegate to a tool.**

Consider the options in increasing order of ambition:

| Check | Catches | Misses |
|---|---|---|
| It booted | Kernel panic, broken initramfs | Everything else |
| systemd reports no failed units | Crashed services, bad unit files | Services that start and do nothing useful |
| The VMS answers its own health endpoint | Application startup failures, bad config | A VMS that runs and records nothing |
| **A camera is actually recording** | Nearly everything that matters | Failures slower than the check window |

**That table is alert design, and it is worth knowing that is what you are doing.** Every row is a signal, and each one down the ladder is *closer to the thing the customer paid for* and *further from the thing that is easy to measure*. The course meets Prometheus in М13; you are writing your first alert rule here, three modules early, and it is the highest-stakes one in the whole course — **it decides whether an operating system update is kept or rolled back.** Nothing else you write gets to make that call.

The general form, which М13 comes back to:

> **Alarm on the product, not on the process.** "The service is up" is a statement about your software. "Footage is being written" is a statement about the customer's building. Only the second one is what they bought, and a dashboard full of the first is how an outage stays green for a fortnight.

#### A health check is not monitoring, and confusing them is dangerous

These look like the same job and they are close to opposites. Get this straight now, because М13 introduces the other one and students merge them by default.

| | **The health check** (this lesson) | **Monitoring** (М13) |
|---|---|---|
| Runs | **on the box** | somewhere else, scraping in |
| Audience | the box itself | a human |
| Decides | whether to keep this OS slot | whether to wake somebody |
| Must work when | **the network is down** | the network is up |
| If it is wrong | the box rolls back, or keeps a broken update | somebody is paged, or isn't |

The dangerous conflation is one direction specifically: **a health check that asks a remote monitoring system whether it is healthy.** Then an unreachable monitoring server means an unhealthy box, and a network fault at the wrong moment rolls back a perfectly good update on every appliance in the fleet at once — an outage that looks like diligence. The check in this lesson touches nothing outside the box, and that is not an accident of convenience.

The reverse also holds: monitoring must not depend on the thing it monitors, which М13 makes into a placement rule.

The fourth is the right answer for a VMS, and it is worth being clear about why the third is not. A video management system that is up, healthy, answering HTTP, and writing zero bytes of video has failed at the only job it has — and it will look perfectly green on any dashboard that stops at the third row.

The counter-pressure is real: the more demanding the check, the longer the box takes to commit, and the more likely a *transient* problem (a camera rebooting at the same moment) triggers a rollback of a perfectly good update. **Pick a window, write it down, defend it.**

Write the check. On the bench, with no cameras, stand in for the recording test with something you can control:

```bash
cat > /usr/local/bin/rauc-health-check <<'EOF'
#!/bin/sh
# Decides whether the running slot is kept. Exit 0 = good.
set -e

# 1. No failed systemd units.
if systemctl --failed --no-legend | grep -q .; then
    echo "health: failed units present" >&2
    systemctl --failed --no-legend >&2
    exit 1
fi

# 2. The VMS answers.
if ! curl -fsS --max-time 5 http://localhost:8000/health > /dev/null; then
    echo "health: VMS health endpoint did not answer" >&2
    exit 1
fi

# 3. Footage is actually arriving. On the bench this is a stand-in
#    for "a segment appeared in the spool in the last N seconds";
#    Lesson 4 replaces it with the real thing.
if ! find /data/spool -name '*.mp4' -newermt '-120 seconds' 2>/dev/null | grep -q .; then
    echo "health: no segment written in the last 120s" >&2
    exit 1
fi

echo "health: OK"
EOF
chmod +x /usr/local/bin/rauc-health-check
```

Note the third check is marked as a stand-in **in the code**, not just in your head. That is the same discipline as `camera_sim.py` back in М8 Lesson 2, and Lesson 4 is where it gets replaced — partially. The real replacement arrives with М9: the recorder exports `recorder_camera_silent_seconds_max` on its own `/metrics`, and [`edgevms/health/rauc-health-check`](./edgevms/health/rauc-health-check) reads it locally when a recorder is present, falling back to this stand-in when one is not. Two details in that version are worth stealing now: the window is `2 × segment length + 60`, because a healthy recorder is silent for a whole segment between closes and a 120-second window would roll back every good update; and a box with **no cameras configured** passes the footage test *and says so*, which is a product decision stated rather than an accident hidden.

Wire it to run on boot, and to mark the slot good only if it passes:

```bash
cat > /etc/systemd/system/rauc-mark-good.service <<'EOF'
[Unit]
Description=Confirm this slot works, or leave it unconfirmed
After=network-online.target vms-agent.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# Give the system time to actually start doing its job before judging it.
ExecStartPre=/bin/sleep 150
ExecStart=/usr/local/bin/rauc-health-check
ExecStartPost=/usr/bin/rauc status mark-good

[Install]
WantedBy=multi-user.target
EOF

systemctl enable rauc-mark-good.service
```

`rauc status mark-good` with no argument means `booted` — it marks the slot you are currently running. It also accepts `other` or an explicit slot name like `rootfs.1`, which matters more than it looks: **a build script that marks the wrong slot good is a rollback mechanism that silently does not work**, and it will pass every test until the day you need it.

`ExecStartPost` only runs if `ExecStart` succeeded. If the health check exits non-zero, nothing is marked, `B_TRY` stays set, and the next boot goes back to A. **The rollback needs no code.** Nothing detects the failure and initiates recovery; recovery is what happens when success is not asserted.

## Step 4 — Failure one: a system that will not boot

Build a bundle whose kernel cannot possibly work.

```bash
# on the host
cd ~/edge-bundle
cp -r content content-broken
# scribble over the kernel inside the image
sudo mount -o loop content-broken/rootfs.ext4 /mnt/img
sudo dd if=/dev/urandom of=/mnt/img/vmlinuz bs=1M count=2 conv=notrunc
sudo umount /mnt/img
sed -i 's/2026.09-1/2026.09-2-broken/' content-broken/manifest.raucm

rauc bundle --cert=~/edge-pki/dev.cert.pem --key=~/edge-pki/dev.key.pem \
  content-broken/ update-broken.raucb
```

It is correctly signed. The signature was never the point — **a signature proves who made an update, never that the update works.** People conflate these constantly.

Install and reboot:

```bash
# in the VM, from slot A
rauc install update-broken.raucb
grub-editenv /boot/efi/grubenv list     # ORDER now "B A", B_OK=0
reboot
```

Watch the console. GRUB attempts B, sets `B_TRY=1`, and the kernel fails to load. Reset the VM (`Ctrl-a` then `c`, then `system_reset`, or just restart QEMU).

Second boot: GRUB finds `B_TRY=1`, skips B, boots A.

```bash
cat /etc/slot-id                       # slot A
grub-editenv /boot/efi/grubenv list    # B_TRY=1, B_OK=0
```

**Record what you saw**: how many failed attempts, how long the total outage was, and what state the environment ended in. That last one is the deliverable's real content — a recovered appliance whose variables are in a state that will not recover *again* is a landmine.

## Step 5 — Failure two: it boots perfectly and does not work

The important one, and the one a bootloader cannot catch.

Take a good bundle and break the application rather than the system — point the VMS at a configuration that cannot work:

```bash
sudo mount -o loop content-broken2/rootfs.ext4 /mnt/img
echo 'KVS_STREAM_NAME=' | sudo tee /mnt/img/etc/vms/agent.env   # empty: agent will fail
sudo umount /mnt/img
```

Bundle it, sign it, install it, reboot.

Slot B boots. The kernel is fine. `systemd` comes up. You can log in. Everything about this system looks healthy — and `rauc-health-check` fails on the third test, because nothing is being recorded.

```bash
systemctl status rauc-mark-good        # failed
rauc status                            # rootfs.1 booted, boot status NOT good
```

Do nothing. Reboot.

You are back in slot A, and **that is a VMS that rescued itself from an update which passed every test a naive design would have run.** Write down what would have happened with a check that stopped at "no failed units": the box would have marked itself good, the old slot would have been overwritten by the next update, and the failure would have been discovered by a customer asking for footage that does not exist.

## Step 6 — Failure three: power loss mid-update

The one that started the module.

Install a good bundle and cut the power partway through the write — from your host, while `rauc install` is running:

```bash
pkill -9 qemu-system-x86_64
```

No shutdown, no signal to the guest, no filesystem sync. As brutal as a pulled plug.

Boot it again:

```bash
cat /etc/slot-id                       # slot A — untouched
rauc status                            # rootfs.1 is inconsistent; rootfs.0 booted and good
```

Slot B holds a partially written image and is worthless. **It does not matter.** Nobody was booting it, `B_OK` was never set, and slot A was never modified. Reinstall the bundle and it overwrites the mess.

Compare that against `apt upgrade` interrupted at the same moment, from Lesson 1 Step 1. Same power cut, same instant. One outcome is a box that boots normally and needs the update retried; the other is a van.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| GRUB boots the same slot regardless of the variables | `load_env` is reading a different file from the one `grub-editenv` writes. Both must name the same path; check the `(hd0,gpt1)` device spec matches your ESP. |
| `grub-editenv: command not found` in the VM | Install `grub-common`. RAUC shells out to this binary — without it the grub backend cannot work at all. |
| Variables reset to defaults on every boot | `save_env` is failing silently, usually because the ESP is mounted read-only by GRUB or the grubenv file was never `create`d. |
| Both slots skipped, GRUB drops to a prompt | Both `TRY` flags are set with no `OK` slot. That is the fall-through case — check the `default=9` block in Step 2 is present and correct. |
| `rauc status mark-good` succeeds but the slot is still not good | You marked the wrong slot. Check whether your script passes `booted`, `other`, or an explicit name. |
| Health check passes on a system you know is broken | The check is too weak — that is the lesson of Step 5, arriving on schedule. Go back to the table in Step 3 and move down a row. |
| The 150-second `ExecStartPre` makes iteration unbearable | Shorten it for the bench and put the real number back before you ship. Note in your record which number you tested with. |

## Recap

- **The absence of a success signal is the failure signal.** Nothing detects that an update failed; a slot is only kept if something inside it actively confirms it works. That is what makes the mechanism robust against failures nobody anticipated.
- GRUB owns the selection logic; RAUC only reads and writes the variables. The reference implementation allows **one attempt per slot**, because GRUB's scripting cannot comfortably do more.
- The grubenv must live outside the redundant partitions, or an update destroys the record of what to boot.
- A health check that stops at "no failed units" would have kept the Step 5 update. For a VMS, the check has to reach **is footage actually being written** — and the cost of that ambition is a longer commit window and a real risk of rolling back over transient faults.
- A signature proves who made an update. It never proves the update works.
- Power loss mid-install costs you a reinstall, not a site visit. That is the entire argument of Lesson 1, now demonstrated rather than asserted.

## Exercises

1. Write the three-failure record properly: for each, the failure induced, the number of boot attempts, the total outage duration, the final state of every grubenv variable, and whether the box could survive a *second* failure immediately afterwards. That last column is the one people forget.
2. Extend `grub.cfg` to allow **three** attempts per slot instead of one. When it gets ugly, stop and write a paragraph on why RAUC ships the one-attempt version.
3. Deliberately mark the wrong slot good: run `rauc status mark-good other` from a freshly installed slot, then reboot twice. Describe the state you end up in and how you would detect it in the field.
4. Make the health check *too strict* — require two cameras when only one exists — and watch a perfectly good update roll back. Then argue for a specific commit window in seconds and defend it against both failure modes.
5. The bench cannot reproduce a hardware-dependent failure: a driver that works in QEMU and not on the real board. Given that, describe what your release process must include that this lesson cannot, and what the first hardware you ship an update to should be.

## Where this is going

The OS plane is now complete: atomic, verified, and self-recovering. What is running *on* it is still hand-started processes.

Lesson 4 brings up the VMS itself under Podman and Quadlet, and draws the boundary that makes both planes work together — OS, application, and the data that must outlive them both. It is also where you meet the failure this module has quietly been carrying since Lesson 1: pull the network cable, and find out what happened to the footage.
