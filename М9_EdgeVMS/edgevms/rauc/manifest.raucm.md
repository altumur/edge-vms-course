# manifest.raucm — the bundle manifest template: compatible, version, verity, one rootfs image

**Role.** Lesson 2, Step 4. Template that `rauc/build-bundle.sh` turns into `content-<version>/manifest.raucm` by substituting `@VERSION@`, optionally rewriting `compatible` for the wrong-hardware proof, and stripping `#` comment lines. `rauc bundle` reads it, adds the image digest and size, and signs the whole. `rauc install` on the box compares it with `rauc/system.conf`.

## Stanza by stanza
### `[update]`
- `compatible=example-vms-appliance-x86-64` — must equal `compatible` in `system.conf` or the device refuses the bundle before touching a slot. This is the hardware lock; `--wrong-hardware` changes it to `…-armhf` to show the refusal.
- `version=@VERSION@` — filled in by the build script (e.g. `2026.09-1`); shown by `rauc info` and `rauc status`, and recorded per slot in `data-directory`.

### `[bundle]`
- `format=verity` — the payload is wrapped with a dm-verity hash tree and verified block by block as it is read, rather than hashed once up front; also what makes streaming installation from an HTTP URL possible. `system.conf` refuses the legacy `plain` format (`bundle-formats=-plain`), so this line is required, not optional.

### `[image.rootfs]`
- `filename=rootfs.ext4` — the slot image inside the bundle, targeted at the `rootfs` slot class (`[slot.rootfs.0]`/`[slot.rootfs.1]` in `system.conf`; RAUC picks the inactive one).
- No `sha256`, no `size` — `rauc bundle` computes and writes them; the comment says so, and the comment is stripped before packing.
