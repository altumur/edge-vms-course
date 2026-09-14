# system.conf — RAUC's view of the box: compatible string, GRUB as bootloader, keyring, two rootfs slots

**Role.** Lesson 2, Step 1. Installed by `bench/build-disk.sh` at `/etc/rauc/system.conf` in slot A before the copy, so it ships in BOTH slots. Read by the `rauc` daemon and CLI on the device at every `rauc install`, `rauc status` and `rauc status mark-good`. Pairs with `boot/grub.cfg` (the variables) and `rauc/manifest.raucm` (the `compatible` string it must match).

## Stanza by stanza
### `[system]`
- `compatible=example-vms-appliance-x86-64` — "a hardware lock, and the cheapest safety feature you will ever ship". A bundle whose manifest says anything else is refused before any slot is written. Put the hardware revision in it; change it when the hardware changes.
- `bootloader=grub` — RAUC's GRUB backend: slot selection is expressed by editing `ORDER`, `<slot>_OK`, `<slot>_TRY` with `grub-editenv`, never by rewriting `grub.cfg`.
- `grubenv=/boot/efi/grubenv` — where those variables live: on the ESP, mounted at `/boot/efi` by `fstab`, outside both slots. `build-disk.sh`/`seed-grubenv.sh` create the file; `grub.cfg` `load_env`s and `save_env`s the same path as `(hd0,gpt1)/grubenv`.
- `data-directory=/data/rauc` — per-slot state (installed version, status, timestamps) on the data partition, "not the deprecated statusfile": it must survive both slots being replaced. `build-disk.sh` creates the directory.
- `bundle-formats=-plain` — the default bundle format is still the legacy `plain`; this removes it from the allowed set, so only `verity` (what `manifest.raucm` declares) is accepted. Refusing it outright means a bundle that was never verity-wrapped cannot sneak in.

### `[keyring]`
- `path=/etc/rauc/keyring.pem` — trust anchors ONLY: the root certificate from `pki/make-ca.sh` (`keyring.pem` is a copy of `ca.cert.pem`). The intermediate/signer certificate travels inside each bundle, so the signer can be rotated without touching a device.

### `[slot.rootfs.0]` / `[slot.rootfs.1]`
- `device=/dev/vda2` / `/dev/vda3` — the two root partitions as the virtio disk exposes them (`bench/boot.sh`, `if=virtio`).
- `type=ext4` — RAUC writes the bundle's `rootfs.ext4` image straight onto the partition.
- `bootname=A` / `B` — the name GRUB knows the slot by: it is the prefix of `A_OK`/`A_TRY`, the value in `ORDER`, and what `rauc.slot=A` on the kernel command line (`grub.cfg`) tells RAUC it booted from.

## Notes
- `build-disk.sh` insists that labels, not device names, are identity, and `grub.cfg`/`fstab` follow that rule; this file names raw devices. On the single-disk bench that is harmless, but on hardware where discovery order can change, `device=/dev/disk/by-partlabel/rootfs0` would be the consistent choice.
- Nothing here mentions the ESP or the data partition as slots; they are deliberately outside RAUC's reach.
