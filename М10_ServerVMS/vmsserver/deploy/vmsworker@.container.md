# vmsworker@.container — the worker as a Quadlet template: `systemctl start vmsworker@w-1`, the instance name is the slot

**Role.** The Podman Quadlet unit for `vmsworker` on М9's box, installed at `/etc/containers/systemd/vmsworker@.container`; Quadlet generates `vmsworker@.service` from it. A template: the instance name after `@` (`%i`) is the slot the worker claims (М10 Lesson 4), so `vmsworker@w-1` and `vmsworker@w-2` are two workers with stable names. It is М9's `worker.container` with Postgres gone — the worker's truth is the platform's stores on the data partition and its archive is the resource. In М11 this unit becomes `vmsworker.nomad.hcl` with `count = N` and the slot from `NOMAD_ALLOC_INDEX`. Checked by `tests/test_deploy_units.py` (image, `Exec`, env file, `/data/` volumes, the archive writable, media read-only, `StopTimeout=20`) and by `check-quadlet.sh` on a box with podman.

## Stanza by stanza

### `[Unit]`
- `Description=VMS worker %i — pipelines against its assignment` — `%i` is substituted per instance in `systemctl status`.
- `After=network.target` — ordering only; the worker needs no network on one box (file stores), but the console's port and М11's stores will.

### `[Container]`
- `Image=localhost/vmsserver:latest` — the one image the Containerfile builds; the test asserts all four units name it.
- `Exec=python3 -m vms worker` — the `worker` verb of `vms/__main__.py` (also the image's default `CMD`, repeated so the unit reads on its own).
- `Environment=WORKER_NAME=%i` — the slot preference: `__main__.worker` reads `WORKER_NAME` and `VmsWorker.claim_slot(prefer=…)` takes exactly that slot by CAS, even from a holder that has not lapsed — systemd is the authority on which process is the current `w-1`, and the old one finds out at its next `renew_slot` and fences.
- `EnvironmentFile=/data/config/vms.env` — `PLATFORM_DIR`, `SPOOL`, `ARCHIVE`, `MEDIA_DIR`, `CAPACITY`, `SEGMENT_SECONDS`, `LOG_LEVEL` (see `vms.env.example.md`); on the data partition, never a rootfs slot.
- `Volume=/data/platform:/data/platform:z` — the two stores, read-write: the worker writes `vms/epoch/*`, `vms/slots/*` (its token) and its heartbeat object. `:z` relabels for SELinux, shared.
- `Volume=/data/spool:/data/spool:z` — where `archivesink` writes segments; `closed_in_spool` reads it at start.
- `Volume=/data/archive:/data/archive:z` — read-write: the worker is the only writer of segments (promote) and of the camera's event buckets; the test asserts this mount has no `ro`.
- `Volume=/data/media:/data/media:ro,z` — the files `driverpack://file/<name>` plays; read-only, the worker never writes media.
- `Network=host` — no port mapping; the worker opens nothing, and in М11 the gateway branch will.
- `StopTimeout=20` — the comment says why: SIGTERM lets every `splitmuxsink` finalize its open segment (the actuator sends EOS before NULL) and the worker `release_slot()`; SIGKILL after 20 s loses exactly that segment (М10 Lesson 3) and leaves the slot to lapse (Lesson 4) — a crash, which the controller does not redistribute.

### `[Service]`
- `Restart=always` — the process supervises its pipelines; systemd supervises the process. A restarted instance claims the same `%i` slot and records from its assignment with the next epoch per camera — no controller involved.
- `RestartSec=2` — two seconds between exit and restart; the failover a fresh instance measures (`previous_hb` → `started`) includes this.

### `[Install]`
- `WantedBy=multi-user.target` — enabled instances start at boot (`systemctl enable vmsworker@w-1`).

## Notes
- One instance per box is the course's setting; nothing forbids `vmsworker@w-2` on the same box — that is how "the zombie on one box" is reproduced by hand with two real processes (start `w-1` twice under different unit names, or pause one).
- What differs from the controller and console units is exactly the mounts: only this unit has `/data/media` and a writable `/data/spool`.
