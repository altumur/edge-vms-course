# seed-grubenv.sh — (re)create the GRUB environment on the ESP with both slots good, A first

**Role.** Lesson 3, Step 2, run once inside the VM (`bench/build-disk.sh` already does the same at build time, so on a fresh bench this is a repair or a reset tool). POSIX `sh`, `set -e`: any failing step aborts. Needs `grub-editenv` from `grub-common`, which the image includes.

## Step by step
### Make the ESP writable
`mount -o remount,rw /boot/efi 2>/dev/null || mount /dev/vda1 /boot/efi` — if the ESP is mounted (fstab mounts it with `nofail`) remount it read-write; if it is not mounted at all, mount partition 1 of the virtio disk at `/boot/efi`. The `||` covers the case where `/boot/efi` was never mounted rather than a remount failure.

### Create and set
`grub-editenv /boot/efi/grubenv create` writes a fresh 1 KiB environment block. `set ORDER="A B" A_OK=1 A_TRY=0 B_OK=1 B_TRY=0` puts the machine in the "both slots known good, boot A first" state that `boot/grub.cfg` reads.

### List
`grub-editenv /boot/efi/grubenv list` prints the result. The comment asks you to run this after every step from here on — it is the window into the whole mechanism (`rauc install`, a failed boot, `mark-good`).

## Notes
- `create` truncates: any `rauc install` history in `ORDER` is lost. That is the intended use (reset), not a side effect.
