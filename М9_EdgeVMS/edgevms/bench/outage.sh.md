# outage.sh — pull the bench's virtual cable for N seconds, or cut its power

**Role.** Lesson 4, Steps 5 and 8 (the uplink outage the spool must survive) and Lesson 3, Step 6 (the pulled plug mid-`rauc install`). Talks to the QEMU monitor socket that `bench/boot.sh` exposes at `$BENCH/monitor.sock` and toggles the link state of `nic0`; the guest sees carrier loss exactly as it would from an unplugged cable. Needs `socat` on the host (README: the fallback is Ctrl-a c and typing `set_link nic0 off` by hand). Exit 2 on a bad argument; otherwise the exit status of the last command.

## Environment variables
- `BENCH` — default `$HOME/edge-bench`; `SOCK` is `$BENCH/monitor.sock`.

## Step by step
### `mon()`
Writes one monitor command followed by a newline to the Unix socket through `socat - UNIX-CONNECT:$SOCK`, discarding the monitor's reply. It is the only way this script touches the VM.

### `off` / `on`
`set_link nic0 off` or `on`, with a one-line confirmation. Manual control for experiments that do not fit a fixed duration.

### `power-cut`
`pkill -9 qemu-system-x86_64`: no shutdown, no flush — the plug is pulled. Used mid-`rauc install` to show that slot A is untouched, slot B is worthless (its `_OK` was never set, so GRUB will not choose it), and the fix is to reinstall. `bench/boot.sh` powers the box back on.

### a number of seconds
The default branch: any all-digit argument is a duration. Link down, `sleep` that long, link up, with a message on each transition. `bench/outage.sh 600` is the ten-minute outage after which Lesson 4 asks you to find the footage in the spool.

### usage
An empty argument or anything containing a non-digit prints usage to stderr and exits 2. The pattern `''|*[!0-9]*` is checked before the catch-all so that only pure integers reach the sleep branch.

## Notes
- `set_link` requires the NIC id `nic0`, which is fixed in `boot.sh`'s `-device virtio-net-pci,...,id=nic0`; renaming it there breaks this script silently (the monitor reply is discarded).
