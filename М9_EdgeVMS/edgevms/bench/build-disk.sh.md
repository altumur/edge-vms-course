# build-disk.sh — build the A/B appliance disk image: ESP | rootfs0 | rootfs1 | data

**Role.** Lesson 1, Steps 4–5 (with Lesson 1 Step 3's partition table and Lesson 3 Step 2's GRUB environment folded in). Produces `~/edge-bench/appliance.qcow2`: a GPT disk with an EFI system partition, two byte-identical Debian root filesystems (slots A and B, differing only in `/etc/slot-id`), and a data partition that outlives both. Everything that must survive an OS update goes on `/data`; everything that must be present after an update goes into the slot image before it is copied. Runs on a Linux host with root via `sudo`, needs `qemu-utils parted e2fsprogs dosfstools debootstrap`, the `nbd` kernel module, and network access to a Debian mirror. Exits 1 if the disk already exists; any other failure aborts through `set -euo pipefail`, and the `trap` unmounts and disconnects the NBD device.

## Environment variables
- `BENCH` — output directory, default `$HOME/edge-bench`.
- `SIZE` — qcow2 virtual size, default `40G`.
- `NBD` — network block device used to expose the qcow2 as partitions, default `/dev/nbd0`.
- `SUITE` — Debian release for `debootstrap`, default `bookworm`. `MIRROR` — default `http://deb.debian.org/debian`.
- `ROOT_PASSWORD` — if set, applied with `chpasswd`; if empty, `passwd root` prompts interactively inside the chroot.
- `HERE` — computed: the `edgevms/` directory, used to find `rauc/`, `quadlet/`, `health/`, `boot/`, `pki/out/`.

## Step by step
### Guard and create the image
Refuses to overwrite an existing `appliance.qcow2` (delete it to rebuild), then `qemu-img create -f qcow2`. The trap registered right after `qemu-nbd --connect` unmounts `dev/proc/sys` bind mounts, the ESP, both slots, and disconnects the NBD device on any exit, with `set +e` so a failed umount does not mask the original error.

### Partitions (Lesson 1, Step 3)
`parted` writes a GPT label and four partitions: `ESP` (FAT32, 1–513 MiB, flagged `esp`), `rootfs0` (513–8705 MiB), `rootfs1` (8705–16897 MiB), `data` (rest of the disk). Both root slots are exactly 8 GiB so a bundle built from A always fits B. Filesystems are made with labels — `ESP`, `rootfs0`, `rootfs1`, `data` — because, as the comment says, "labels are identity; device names are discovery order": `grub.cfg` selects a slot by `search --label` and `fstab` mounts by `LABEL=`. `lsblk -f` output is saved to `$BENCH/healthy-lsblk.txt` as a record of what healthy looked like, which Lesson 3 uses when diagnosing a broken update.

### Slot A: debootstrap
Mounts `p2` at `/mnt/slotA` and runs `debootstrap --arch=amd64` with `--include=linux-image-amd64,grub-efi-amd64,grub-common,systemd-sysv,rauc,podman,curl,ca-certificates,python3`. `rauc` is the updater, `podman` runs the Quadlet units, `grub-common` supplies `grub-editenv` (the "window into the whole mechanism"), `curl` and `python3` are what `health/rauc-health-check` needs. Root password is set per `ROOT_PASSWORD`. `/etc/slot-id` is written as `slot A` — the only byte that will differ between slots.

### Files that must be IN THE IMAGE (Lesson 4)
Installed into slot A before the copy to B, because "the moment you type it by hand on a running box is the moment it is missing from slot B":
- `rauc/system.conf` → `/etc/rauc/system.conf`
- `quadlet/storage.conf` → `/etc/containers/storage.conf` (graphroot on `/data`)
- `quadlet/vms-agent.container` → `/etc/containers/systemd/vms-agent.container`
- `health/rauc-health-check` → `/usr/local/bin/rauc-health-check` (0755)
- `health/rauc-mark-good.service` → `/etc/systemd/system/`, then `systemctl enable` inside the chroot — correct for a plain unit, and explicitly not done for Quadlet files, whose generator applies `[Install]` itself.
- `pki/out/keyring.pem` → `/etc/rauc/keyring.pem`, only if `pki/make-ca.sh` has already run; otherwise Lesson 2 copies it by hand the first time.

### fstab and /data
`/etc/fstab` mounts `LABEL=data` at `/data` and `LABEL=ESP` at `/boot/efi`, both `nofail` so a missing partition degrades instead of dropping to an emergency shell. No root entry: the kernel gets `root=LABEL=…` from GRUB. `/data` mount point is created in the slot.

### GRUB to the ESP, by hand
Mounts the ESP at `/mnt/slotA/boot/efi`, bind-mounts `dev/proc/sys`, and runs `grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=BOOT --removable` in the chroot. `--removable` installs to the fallback path `EFI/BOOT/BOOTX64.EFI`, which firmware boots without an NVRAM entry — the right choice for an image that is cloned. `boot/grub.cfg` is copied next to it; `update-grub` is never run (the header of `grub.cfg` forbids it). Then, Lesson 3 Step 2: `grub-editenv /boot/efi/grubenv create` and `set ORDER="A B" A_OK=1 A_TRY=0 B_OK=1 B_TRY=0` — both slots known good, A first — and `list` to print it. The environment lives on the ESP, outside both slots. Bind mounts and the ESP are unmounted; the ESP is not part of a slot.

### Slot B: a copy
`cp -a /mnt/slotA/. /mnt/slotB/`, clear the (empty) `boot/efi` mount point contents, write `slot B` to `/etc/slot-id`. This is what the factory does: two identical slots.

### Data partition
Mounted temporarily under slot A's `/data` to create `config/`, `spool/`, `rauc/`, `containers/storage/`, `archive/`, `pg/` — the directories `system.conf` (`data-directory`), `storage.conf` (`graphroot`), the Quadlet volumes and the recorder expect to exist. Then everything is unmounted and the path and next step (`bench/boot.sh`, choose Slot B at the menu to prove independence) are printed.

## Notes
- The README states the image "installs `system.conf`, `storage.conf`, the Quadlet units, the health check and `spool.py` into slot A". The script installs only `vms-agent.container` among the Quadlet units — not `spool-uploader.container`, `postgres.container` or `worker.container` — and does not copy `spool.py` (which reaches the box inside the agent image at `/app/spool.py`). On a fresh bench the uploader, Postgres and worker units must be added by hand or by a bundle.
- The bind-mount cleanup in the trap covers slot A's `dev/proc/sys`, the ESP and both slots, but not `/mnt/slotA/data`; that mount is only unmounted inline, so a failure between lines 102 and 104 leaves it mounted.
- The script marks the ESP with `set 1 esp on`; OVMF finds `EFI/BOOT/BOOTX64.EFI` on it by the removable-media rule, which is why no `efibootmgr` entry is needed.
