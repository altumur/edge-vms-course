# build-bundle.sh — build and sign a RAUC bundle from the bench's slot A, optionally broken on purpose

**Role.** Lesson 2, Step 4 and Lesson 3, Steps 4–5. Extracts slot A of `appliance.qcow2` once into `~/edge-bundle/rootfs.ext4`, fills in `rauc/manifest.raucm`, optionally sabotages the image, and runs `rauc bundle` with the CI signer from `pki/out`. The lesson it carries: a signature proves who made an update, never that it works — the `--broken-*` bundles are signed with the real key. Runs on the host; needs `rauc`, `qemu-nbd`, `e2fsprogs`, `sudo`, and a finished `pki/make-ca.sh`. `set -euo pipefail`; exit 2 on an unknown option; `rauc` errors propagate.

Usage: `build-bundle.sh <version> [--broken-kernel|--broken-config] [--rogue] [--wrong-hardware]`.

## Environment variables
- `PKI` — default `pki/out`; `CERT`/`KEY` default to `dev.cert.pem`/`dev.key.pem` there.
- `BENCH` — default `$HOME/edge-bench` (source of `appliance.qcow2`).
- `WORK` — default `$HOME/edge-bundle`; holds `rootfs.ext4`, `content-<version>/`, `update-<version>.raucb`.
- `ROOTFS` — the extracted slot image, default `$WORK/rootfs.ext4`.
- `NBD` — default `/dev/nbd0`.

## Step by step
### Options
`VERSION` is the required first argument. The loop over the rest sets: `--rogue` → sign with `rogue.cert.pem`/`rogue.key.pem` (Lesson 2 Step 6, wrong signer, refused by the device before anything is written); `--wrong-hardware` → `COMPAT_SED='s/x86-64/armhf/'` so `compatible` in the manifest no longer matches `system.conf` (Lesson 2 Step 6); `--broken-kernel` / `--broken-config` → remembered in `BREAK` (Lesson 3 failures one and two). Anything else is an error.

### 1. Extract slot A once
If `ROOTFS` is missing: `modprobe nbd`, `qemu-nbd --connect`, `dd` partition 2 to the file, disconnect, `chown` to the user, `e2fsck -fy` (errors tolerated), `resize2fs -M` to shrink the 8 GiB image to its contents — "an 8 GB file is a slow lesson". The file is reused by every later build, so a rebuilt bench needs a deleted `rootfs.ext4`.

### 2. Content directory
`content-<version>/` is recreated; `rootfs.ext4` is copied in with `--reflink=auto` (free on btrfs/XFS); the manifest template has `@VERSION@` replaced, the compatible string rewritten if asked, and comment lines removed (`grep -v '^#'`) because RAUC's manifest parser does not want them.

### 3. Break it, if asked
The image is loop-mounted and modified *inside*, so the signature over it is still perfectly valid:
- `--broken-kernel` — 2 MiB of `/dev/urandom` over `/vmlinuz` (through the symlink) and, after `readlink -f`, over its target as well. The result will not boot: GRUB tries B, sets `B_TRY=1`, the kernel dies, next boot is A.
- `--broken-config` — `sed` replaces `Image=` in `/etc/containers/systemd/vms-agent.container` with a non-existent image. The lesson describes breaking `agent.env`, but that file lives on `/data` and is in no bundle, so the script breaks "the thing that is": the box boots perfectly and records nothing, which only the health check's third row catches (README "Known gaps").
Then unmount and remove the mount point.

### 4. Pack and sign
`rauc bundle --cert=$CERT --key=$KEY content-<version>/ update-<version>.raucb`, followed by `rauc info --keyring=pki/out/keyring.pem` on the result — which refuses a `--rogue` bundle, and the script says so instead of failing. Prints the `scp -P 2222 … root@127.0.0.1:/data/` and `rauc install /data/…` lines for the VM.

## Notes
- The manifest's `format=verity` means `rauc bundle` also computes the dm-verity hash tree and writes `sha256`/`size` into the manifest; that is why `manifest.raucm` omits them.
- `readlink -f "$M/vmlinuz"` resolves the symlink on the *host*; Debian's `/vmlinuz` is a relative symlink (`boot/vmlinuz-…`), so it resolves inside the mount as intended. An absolute symlink would point at the host's own `/boot`; the guard only runs `dd` if `readlink` succeeded, and the first `dd` already went through the symlink, so the second is belt and braces.
- `rootfs.ext4` is whatever slot A held when it was first extracted — including `/etc/slot-id` = `slot A`. Installed into B, the box will say "slot A" in `/etc/slot-id`; the kernel command line (`rauc.slot=B`) is the authoritative identity.
