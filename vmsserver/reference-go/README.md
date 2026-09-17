# reference-go — the Go port of the halves that are Python

A snapshot of the complete Go port, from before the system settled on Python for the platform and Go for
the workers. It is kept for one reason: it says, in working code, what the controller, the console and the
resource look like when written in another language — and that is worth having beside a course whose
claim is that the contract is data and either half can be rewritten.

**It is not in CI, and it is not the live Go code.** The live Go code is `../worker/`, which is a cut of
this tree: the contract, the unit half of the spec, the archive tree, and the two workers.

It still builds and its tests still pass as of the commit that put it here:

```
go build ./... && go test ./...      # 88
```

It will drift. Nothing keeps it in step with `../w2cplatform/` and `../vms/`, and no test will say so —
that was the price of keeping it, and it was paid knowingly. Read it as a port, not as a second
implementation anyone maintains. `w2cplatform/contract.go`, `unit.go`, `heartbeats.go`, `space.go` and
`vms/archive.go` exist here as copies of files that live, current, under `../worker/`; the ones that exist
only here are the ones worth reading:

```
w2cplatform/placement.go      SpecController: pool, filters, near and home, redistribute, rebalance
w2cplatform/console.go        the console as data, and the Mount with /drain and /schema
w2cplatform/eventdatabase.go  the index over a resource's buckets, and the merge across resources
w2cplatform/resource.go       the resource process: heartbeat, policy pass, mirroring, watermark
vms/controller.go             the VMS's vocabulary over SpecController
vms/console.go                the one-box console
vms/resource.go               the VMS's routes on the resource
vms/space.go                  evacuation and the cut
vms/archivepolicy.go          repair, retain, free
```
