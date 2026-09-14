# EdgeVMS — the М9 artifacts, whole

One project, two halves. Lessons 1–4 are the box: the bench, the PKI, the
RAUC configuration and bundle builder, the GRUB state machine, the health
check, the Quadlet units, and the spool. Lessons 5–9 are what runs on it:
[`recorder/`](recorder/README.md) — the migrations, the worker, the console,
the commissioning tools and 27 tests. The Quadlet units for both live in one
`quadlet/`, because on the box they are one `systemd`.

```
edgevms/
  bench/
    build-disk.sh          L1 — ESP | rootfs0 | rootfs1 | data; debootstrap slot A; copy to B; GRUB by hand
    boot.sh                L1 — QEMU/OVMF with KVM auto-detect, a monitor socket, port forwards
    outage.sh              L4 — pull the cable for N seconds; L3 — the pulled plug (power-cut)
  pki/
    make-ca.sh             L2 — two-level CA; keyring = the root only; and the attacker's CA
    verify-chain.sh        L2 — the three proofs with `openssl cms`: accepted, wrong signer, tampered
  rauc/
    system.conf            L2 — compatible, data-directory, bundle-formats=-plain
    manifest.raucm         L2 — verity, no sha256/size
    build-bundle.sh        L2/L3 — good, --broken-kernel, --broken-config, --rogue, --wrong-hardware
  boot/
    grub.cfg               L3 — ORDER / OK / TRY; one attempt per slot; env on the ESP
    seed-grubenv.sh        L3 — grub-editenv create + set
  health/
    rauc-health-check      L3 — the ladder, with row 3 now REAL: the recorder's own signal
    rauc-mark-good.service L3 — ExecStartPost only on success; the rollback needs no code
  quadlet/
    storage.conf           L4 — graphroot on /data, in the image
    vms-agent.container    L4 — the М8 agent under systemd
    spool-uploader.container   L4 — the uploader as its own process
    agent.env.example      L4 — the first temporary secret, named
    check-quadlet.sh       L4 — generator --dryrun, not systemd-analyze
    postgres.container     L5 — the recorder's database, ordered before the worker
    worker.container       L6–9 — the worker: reconcile, pump_buses, report, retention, console; SIGTERM finalises segments
    worker.env.example     L5 — DATABASE_URL and the rest, hand-provisioned (the stand-in М12 resolves)
  spool/
    spool.py               L4 — Spool, drain, the two signals, and the uploader main loop
    test_spool.py          L4 — the five tests, plus one for the signals
  recorder/                L5–9 — the recorder itself: worker/, console/, migrations/, tools/, tests/ (its own README)
```

## The seam this closes

Lesson 3's health check has to reach *is footage actually being written*,
and Lesson 3 was written before the recorder existed, so its third row was a
stand-in: "a segment appeared in the spool in the last N seconds". That
check is weak on purpose (a fast uploader empties the spool) and the lesson
marks it.

`health/rauc-health-check` now prefers the real thing. М9's recorder exports
`recorder_camera_silent_seconds_max` on `127.0.0.1:8080/metrics` — the same
number М13 alarms on — and the health check reads it **locally**, with the
uplink down, and decides:

| recorder says | Verdict |
|---|---|
| Worker not reporting | fail — the console is up and nobody is home |
| cameras configured, none has ever written a segment | fail — boots perfectly, records nothing (Lesson 3, failure two) |
| newest segment older than `2 × SEGMENT_SECONDS + 60` | fail |
| no cameras configured | pass, and says so — a stated product decision, not a hidden one |
| otherwise | pass |

Without a recorder (an М9-only bench) it falls back to the agent's `/health`
plus the spool stand-in, exactly as Lesson 3 wrote it. It touches nothing
outside the box either way.

The window is `2 × SEGMENT_SECONDS + 60` because a healthy recorder is silent
for up to one segment length between closes; a 120-second window against
ten-minute segments would roll back every good update. `rauc-mark-good.service`'s
`ExecStartPre` sleep must clear a segment length for the same reason.

## Running it

Host: Linux with `qemu-system-x86 qemu-utils ovmf parted e2fsprogs dosfstools debootstrap socat rauc openssl`.

```bash
pki/make-ca.sh && pki/verify-chain.sh          # Step 3 of Lesson 2, two minutes, no VM
bench/build-disk.sh                             # builds ~/edge-bench/appliance.qcow2 (asks for a root password)
bench/boot.sh                                   # Ctrl-a x quits. ssh -p 2222 root@127.0.0.1 works.

# Lesson 2
rauc/build-bundle.sh 2026.09-1
scp -P 2222 ~/edge-bundle/update-2026.09-1.raucb root@127.0.0.1:/data/
#   in the VM:   rauc install /data/update-2026.09-1.raucb && rauc status && grub-editenv /boot/efi/grubenv list
rauc/build-bundle.sh 2026.09-1-rogue --rogue     # refused before writing anything
rauc/build-bundle.sh 2026.09-1-armhf --wrong-hardware

# Lesson 3: three failures
rauc/build-bundle.sh 2026.09-2-broken --broken-kernel     # GRUB tries B, kernel dies, next boot is A
rauc/build-bundle.sh 2026.09-3-broken --broken-config     # boots perfectly, records nothing, health check fails, next boot is A
bench/outage.sh power-cut                                 # mid rauc install: A untouched, B worthless, reinstall

# Lesson 4
bench/outage.sh 600                                       # ten minutes without an uplink; then find the footage
python3 spool/test_spool.py
```

The bench image includes `rauc`, `podman`, `grub-common`, `curl` and
`python3` (build-disk.sh's `--include`), and installs `system.conf`,
`storage.conf`, the Quadlet units, the health check and `spool.py` into
slot A **before** it is copied to B — Lesson 4's rule that the moment you
type it by hand on a running box is the moment it is missing from the other
slot.

## What was verified where

- **Run, output real:** `pki/make-ca.sh` and `pki/verify-chain.sh` against OpenSSL 3.0.13 (the three refusals come out exactly as Lesson 2 prints them; note `openssl cms -verify` exits 4 on a refusal, which is why the script judges by message). `spool/test_spool.py`, six tests. `health/rauc-health-check` against a fake recorder serving seven `/metrics` scenarios — healthy, silent 45 min, never recorded, Worker not reporting, no cameras, no recorder and no agent, agent-plus-spool — each giving the verdict in the table above.
- **Written to the documentation, not executed here:** `bench/build-disk.sh` (needs KVM/nbd/debootstrap and root), `rauc/build-bundle.sh` (needs `rauc`), `boot/grub.cfg` (RAUC's reference logic, copied from Lesson 3), the Quadlet units (need Podman's generator — `quadlet/check-quadlet.sh` is the check). They pass `bash -n`; the first run belongs on your bench.

## Known gaps, named

- `--broken-config` breaks the Quadlet `Image=` inside the image, not `agent.env` as Lesson 3 describes, because `agent.env` lives on `/data` and is not in any bundle. Same failure — boots perfectly, records nothing — reached the only way a bundle can reach it.
- `vms-agent.container` and `spool-uploader.container` run `localhost/example/vms-agent:1.0`, built by `make agent-image` in [`М8_KVS_VMS/kvsvms/`](../../М8_KVS_VMS/kvsvms/README.md) from the course root. That image carries `/health`, this directory's `spool.py` at `/app/spool.py`, and `vms-upload-segment` — the acknowledged uploader (`kvsvms/edge/upload_segment.py`): a `kvssink` re-publish in offline mode with the segment's original start time, followed by `ListFragments` over the segment's span. Exit 0 only when the archive has it.
- `outage.sh` needs `socat` for the monitor socket; `Ctrl-a c` and `set_link nic0 off` by hand is the fallback.
