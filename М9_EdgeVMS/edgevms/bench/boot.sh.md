# boot.sh — boot the bench VM under QEMU/OVMF with a monitor socket

**Role.** Lesson 1, Step 6. Starts the appliance image that `build-disk.sh` produced as a UEFI virtual machine, on the console (`-nographic`), with the SSH port and the console port forwarded to the host and the QEMU monitor exposed on a Unix socket so `bench/outage.sh` can pull the virtual cable or cut the power without a human at the keyboard. It runs on the developer's Linux host, needs `qemu-system-x86_64` and the OVMF firmware files, and `exec`s into QEMU, so its exit status is QEMU's. Ctrl-a x quits, Ctrl-a c switches to the monitor.

## Environment variables
- `BENCH` — bench directory, default `$HOME/edge-bench`; holds `appliance.qcow2`, the OVMF variable store and `monitor.sock`.
- `OVMF_CODE` — read-only UEFI firmware image, default `/usr/share/OVMF/OVMF_CODE.fd`.
- `OVMF_VARS` — template for the writable UEFI variable store, default `/usr/share/OVMF/OVMF_VARS.fd`; copied into `$BENCH/OVMF_VARS.fd` once, so boot-order changes persist across runs of the bench but never touch the system copy.
- `MEM` — guest RAM in MiB, default 2048. `SMP` — vCPUs, default 2.

## Step by step
### Locate disk and firmware
`DISK` is fixed at `$BENCH/appliance.qcow2`. If there is no per-bench `OVMF_VARS.fd` yet, it is copied from the template: the variable store is the one piece of firmware state the VM writes, and it must be private to the bench.

### KVM auto-detect
If `/dev/kvm` is readable the array `KVM` becomes `-enable-kvm`; otherwise it stays empty and a warning goes to stderr ("full emulation, slower, still correct"). The lesson's point: the bench is about correctness of the A/B mechanism, not speed, so the absence of KVM is not a failure.

### exec qemu-system-x86_64
- Two `pflash` drives: the read-only firmware code and the writable variable store. This is what makes the VM boot UEFI, which the whole GRUB-on-the-ESP design (`boot/grub.cfg`) assumes.
- `-drive file=$DISK,format=qcow2,if=virtio` — the disk appears as `/dev/vda`, which is why `rauc/system.conf` names `/dev/vda2` and `/dev/vda3` and `seed-grubenv.sh` falls back to `/dev/vda1`.
- `-netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22,hostfwd=tcp:127.0.0.1:8080-:8080` — user-mode networking with two host forwards: `ssh -p 2222 root@127.0.0.1` reaches the box, and `localhost:8080` reaches the recorder console (`CONSOLE_PORT` in `quadlet/worker.env.example`). Bound to loopback only.
- `-device virtio-net-pci,netdev=net0,id=nic0` — the NIC is named `nic0` on purpose: `outage.sh` issues `set_link nic0 off|on` through the monitor.
- `-monitor unix:$BENCH/monitor.sock,server,nowait` — the monitor on a socket, created as a server without waiting for a client, so the VM boots whether or not anyone connects.
- `-nographic` — everything on the serial console, which matches `console=ttyS0,115200` in `grub.cfg`.
- `"$@"` — any extra arguments are passed straight to QEMU.

## Notes
- The script never creates the disk; a missing `appliance.qcow2` is a QEMU error, and the fix is `bench/build-disk.sh`.
- Because `exec` replaces the shell, the `monitor.sock` file is left behind after QEMU exits; QEMU recreates it on the next boot.
