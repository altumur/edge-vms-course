# storage.conf — Podman's graphroot on the data partition, in the image, both slots

**Role.** Lesson 4, Step 1. Installed by `bench/build-disk.sh` at `/etc/containers/storage.conf` in slot A before the copy to B, so every slot agrees where images and volumes live. Read by Podman (and the Quadlet-generated services) on every invocation. The reason is the header's: Podman's default graphroot is inside the root filesystem, so everything works until the first OS update overwrites the slot holding every image and volume — on a box that then boots successfully, so nothing rolls back.

## Stanza by stanza
### `[storage]`
- `driver = "overlay"` — the standard overlay layer store.
- `graphroot = "/data/containers/storage"` — images, layers and named volumes on `/data` (`build-disk.sh` creates the directory). An update replaces the slot and finds its images where it left them.
- `runroot = "/run/containers/storage"` — runtime state (container pids, mounts) on tmpfs, gone at reboot, which is correct: containers do not survive a reboot, images do.

## Notes
- The recorder's Postgres data is a bind mount of `/data/pg`, not a named volume, so it does not depend on this file; the images themselves do.
