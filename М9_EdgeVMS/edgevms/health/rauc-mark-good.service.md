# rauc-mark-good.service — run the health check after boot; mark the slot good only if it passes

**Role.** Lesson 3, Step 3. A plain systemd unit (not Quadlet), installed by `bench/build-disk.sh` into `/etc/systemd/system/` and enabled with `systemctl enable` in the chroot. It is the only code between "the slot booted" and "the slot is confirmed": `ExecStartPost` runs only if `ExecStart` succeeded, so a failing `rauc-health-check` leaves `<slot>_TRY=1` in the GRUB environment and the next boot goes back to the other slot. The rollback needs no code.

## Stanza by stanza
### `[Unit]`
- `Description=Confirm this slot works, or leave it unconfirmed`.
- `After=network-online.target worker.service vms-agent.service vmsworker@w-1.service vmsconsole.service` — order after the things the check will interrogate: the М9 recorder (`quadlet/worker.container` → `worker.service`), the М8 agent (`vms-agent.container`), and М10's worker and console units. `After=` without `Requires=` means a missing unit is not an error, which is what lets one service file serve every variant of the box.
- `Wants=network-online.target` — pull in network readiness, though the check itself never leaves the box.

### `[Service]`
- `Type=oneshot` with `RemainAfterExit=yes` — runs once per boot and then shows `active (exited)`, so `systemctl status` tells you whether this boot was judged.
- `ExecStartPre=/bin/sleep 150` — give the system time to actually start doing its job before judging it. The comment says it must exceed one segment length (М9 Lesson 7) plus startup and that "the bench shortens it".
- `ExecStart=/usr/local/bin/rauc-health-check` — the ladder; exit 0 = good.
- `ExecStartPost=/usr/bin/rauc status mark-good` — clears the running slot's `TRY` flag through `grub-editenv`; only reached on success.

### `[Install]`
- `WantedBy=multi-user.target` — enabled at boot.

## Notes
- With the shipped defaults there is a mismatch: `SEGMENT_SECONDS` is 600 in `quadlet/worker.env.example`, so the recorder's first segment closes about ten minutes after start, but this unit judges at 150 s. On a box whose cameras were all added recently, `recorder_cameras_never_recorded` equals `recorder_cameras` at that moment and the health check fails ("no camera has written a segment") — an update that is fine gets rolled back. Either the sleep must clear a segment length (as the README says: "must clear a segment length for the same reason") or the bench must run with short segments. Cameras that have older segments in the index are unaffected, since the view looks back two hours.
- Because the unit is `oneshot`, a slot that failed the check is not retried within the same boot; the retry is the next boot on the other slot.
