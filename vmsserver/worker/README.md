# worker — the Go half

Two processes: `vms worker` and `vms recorder`. Everything else in this system — placement, the console,
the resource's policy pass — is Python, one directory up.

The cut is not a matter of taste. Between a controller and a worker there is no RPC: they meet in the
store, and the worker's whole surface there is four families of keys.

```
<sub>/slots/<worker>       CAS write   the slot: who holds it, until when, whether it was let go of on purpose
<sub>/epoch/<unit>         CAS write   the epoch, and the lease renewed against it
<sub>/workers/<worker>     read        the assignment
<sub>/<worker>/heartbeat   write       the heartbeat, as an object
```

plus the unit's own row, read. That is all, which is why this half can be written in another language at
all, and why doing so costs no coordination beyond keeping those four shapes byte for byte.

## What is here, and what is deliberately not

`w2cplatform/` is the platform's **contract**, not the platform:

| file | what it is |
|---|---|
| `contract.go` | subsystems, slots, epochs, leases, assignments, heartbeats, schema and build |
| `heartbeats.go` | reading heartbeats: the read model, and the freshness rule a caller uses before it talks to a holder |
| `unit.go` | what a row IS: the spec's `unit` block — fields, types, defaults, the id rule |
| `space.go` | the disk and the two marks on it: a worker asks before it fetches, a resource before it frees |
| `variables.go` `objects.go` `epoch.go` `events.go` `runtime.go` | the store, the event log, the process loop |

There is **no** `placement.go` and no console here. The spec's `placement` block — `constraint`,
`spread_by`, `near`, `home`, the tie-break, redistribution, rebalancing — is read by the Python
controller and by nothing in this module. A worker reads its assignment and does what it says; it has no
opinion about how the assignment was arrived at. `vms.subsystem.yaml` and `rec.subsystem.yaml` are still
here, and still the same files, because the `unit` half of each is what parses a row.

## The tests

51, and they state the worker's contract rather than a second implementation of someone else's policy:
the setup writes rows and assignments straight into the store, because that is all a worker can ever see
of a controller. `p.Controller` appears in them — it is not placement, it is the contract's own
bookkeeping, writing exactly the rows the Python controller writes.

A green suite here and a green suite in Python prove each half self-consistent and nothing whatever about
whether the two agree. That is what `../tests/test_cross_go_worker.py` is for.

```
go test ./...          # 51
go test -race ./...
```
