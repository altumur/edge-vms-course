# spool-uploader.container — the spool uploader as its own systemd service, delete on acknowledgement

**Role.** Lesson 4, Step 6. Quadlet unit for the process that drains `/data/spool` into KVS (`spool/spool.py` main loop). It is a SEPARATE process from capture (`vms-agent.container`): delete-on-acknowledgement needs somebody other than the writer to do the deleting. It runs the same image as the agent with a different `Exec=`, so there is one thing to publish and version.

## Stanza by stanza
### `[Unit]`
- `Description=Spool uploader (delete on acknowledgement)`.
- `After=network-online.target`, `Wants=network-online.target` — start once the uplink is plausibly up; the uploader tolerates it being down (stop at first failure, retry next tick).

### `[Container]`
- `Image=localhost/example/vms-agent:1.0` — the М8 agent image (`make agent-image` in `М8_KVS_VMS/kvsvms/`), which carries `/app/spool.py` (this directory's `spool/spool.py`) and the `vms-upload-segment` command (README "Known gaps").
- `EnvironmentFile=/data/config/agent.env` — the AWS credential and stream name (`agent.env.example`); the upload command needs them.
- `Volume=/data/spool:/data/spool:z` — the spool, shared with the agent that writes it.
- `Volume=/run/vms:/run/vms:z` — where `spool.py` writes its two signals (`--signals` default `/run/vms/spool-signals`) so a health check or exporter on the host can read them with the uplink down.
- `Exec=python3 /app/spool.py --root /data/spool --max-bytes 345600000000 --policy drop-oldest --budget 2 --tick 5 --upload-cmd "vms-upload-segment"` — the uploader main loop with Lesson 1's number as the bound: 8 cameras × 4 Mbit/s × 24 h ≈ 346 GB. `drop-oldest` is the policy for reaching it; two segments per five-second tick is the rate-limited catch-up; `vms-upload-segment <path>` exits 0 only once the archive has the segment (a `kvssink` re-publish in offline mode followed by `ListFragments` over the span), and that exit status is the acknowledgement.

### `[Service]`
- `Restart=always`, `RestartSec=5` — a crashed uploader is restarted; the spool on disk is its only state, so nothing is lost.

### `[Install]`
- `WantedBy=multi-user.target`.

## Notes
- `bench/build-disk.sh` does not install this unit into the image (only `vms-agent.container`), although the README implies all Quadlet units are baked in.
