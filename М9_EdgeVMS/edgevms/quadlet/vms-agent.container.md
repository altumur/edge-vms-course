# vms-agent.container — the М8 edge agent under systemd via Quadlet

**Role.** Lesson 4, Step 2. Quadlet unit at `/etc/containers/systemd/vms-agent.container` (installed into the image by `bench/build-disk.sh`); the generator emits `vms-agent.service`. Never `systemctl enable` it — the generator applies `[Install]` itself. It runs the М8 capture agent (`localhost/example/vms-agent:1.0`), which writes segments into the spool and answers `/health` on port 8000 — the URL `health/rauc-health-check` falls back to on an М9-only bench. Both volumes point into `/data`: the spool and the configuration are data, and the container is not allowed to hold either. `rauc/build-bundle.sh --broken-config` breaks this file's `Image=` inside a bundle to produce the update that boots and records nothing.

## Stanza by stanza
### `[Unit]`
- `Description=VMS edge agent`.
- `After=network-online.target`, `Wants=network-online.target` — the agent publishes to KVS when it can; ordering after the network avoids a burst of early failures.

### `[Container]`
- `Image=localhost/example/vms-agent:1.0` — built by `make agent-image` in `М8_KVS_VMS/kvsvms/` and stored under `/data/containers/storage` (`storage.conf`). A non-existent image here is Lesson 3's second failure.
- `EnvironmentFile=/data/config/agent.env` — the AWS credential and stream name (`agent.env.example`), on the data partition.
- `Volume=/data/spool:/data/spool:z` — where capture writes `<camera>/<timestamp>.mp4`; shared read-write with `spool-uploader.container`.
- `Volume=/data/config:/data/config:ro,z` — configuration is readable, never writable, from inside the container.
- `PublishPort=127.0.0.1:8000:8000` — `/health` on loopback for the health check; nothing published off-box.

### `[Service]`
- `Restart=always`, `RestartSec=5` — a crashed agent restarts after five seconds; `systemctl --failed` (row 1 of the health check) will still show a unit that keeps failing.

### `[Install]`
- `WantedBy=multi-user.target`.
