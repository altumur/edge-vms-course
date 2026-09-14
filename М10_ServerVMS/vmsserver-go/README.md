# vmsserver-go — М10, whole, in Go

М10's platform and its first subsystem, ported to Go with the Python suite ported alongside, unchanged in meaning. It exists so that [М11's Go port](../../М11_ClusterVMS/clustervms-go/README.md) can be built **on** it the way `clustervms/` is built on `vmsserver/` in Python — one module imported, not copied — and so that the shape (a platform that knows nothing about cameras; a subsystem that is only a prefix, a controller and a worker) can be checked in a second language.

```
vmsserver-go/
  go.mod                       module vmsserver; standard library only
  psimplatform/                 the platform — no import of vms/, and not the word "camera" (a test greps for it)
    variables.go               Variables: the interface (Get/Put/List/Delete, ModifyIndex, cas) and FileVariables (one box, files, flock)
    objects.go                 ObjectStore: the interface and FsObjectStore
    epoch.go                   NextEpoch by CAS; Lease (Renew = read my epoch; MayWrite on a monotonic clock)
    contract.go                Subsystem, Assignment, Heartbeat, Slot; Controller (Write by CAS, WorkersSeen, Assign*, Slots, Retire);
                               Worker (ClaimSlot / RenewSlot / ReleaseSlot, TakeEpoch, RenewLeases, HeartbeatWith)
    events.go                  EventLog: buckets <sub>/<unit>/e<epoch>/<start>Z.events.jsonl; BucketsUnder, SubsystemsUnder
    resource.go                Resource: heartbeat, Retain by <sub>/retention[/<unit>], Mirror to PeersOf, Restore, Pass; Serve over HTTP
    eventindex.go              EventIndex: Rebuild / Tail / Query / Forget over every resource's buckets; the mirror branch
    spec.go  yaml.go           the controller as data: SubsystemSpec (rows, fields, derived rows, placement by name, snapshot), SpecController — the one
                               controller every subsystem runs; a YAML subset parser (block and flow, scalars, comments) so the spec needs no dependency
    console.go  console.html   the console as data: SpecConsole over the same spec — the page (embedded, built from /spec), /<rows>, /where, /metrics with
                               the subsystem's prefix, /marks, the writes with the spec's refusals; a subsystem registers an Extra for its own routes
  vms/                         the VMS — a subsystem
    vms.subsystem.yaml         the VMS's controller, as a spec — embedded into the binary (go:embed); the same file the Python package reads
    config.go                  Camera, the typed view of a spec row; Row / ItemsOf through the spec
    reconciler.go              М9 Lesson 6's loop: Reconcile, Lost, Clear, Status
    archive.go                 SegmentPath / Parse, Manifest (Read / Buckets / Rewrite / Timeline), ArchiveResource (Promote,
                               CloseBuckets, Repair, Retain), ArchivePolicy — the hook the VMS registers with the resource
    worker.go                  VmsWorker: the slot, server, labels and capacity from the environment (a box or an allocation); the gate (an epoch
                               per start), LeasePass, Fence, Observe, PumpOnce, Status, Headroom, Run
    controller.go              VmsController: the SpecController in the VMS's words — CreateCamera / Cameras / Placement with int ids
    console.go                 the one-box console: the platform's SpecConsole over the VMS spec plus VmsRoutes — /timeline/<id> and /segment/<path> with Range
  gstvms/uri.go                driverpack://file/<name> resolution — the pure part; the element itself is Python's (GStreamer)
  testbox/                     the fixture both Go suites share: FileVariables + FsObjectStore in a temp dir, a spool, an archive, two clocks
  cmd/vms/main.go              vms worker|controller — the two processes on one box, with the fake actuator
  *_test.go                    44 tests, in the packages they test; -race clean
```

```bash
go test ./...                    # 44 tests, ~130 ms
go test -race ./...              # the CAS races with real goroutines
go build ./cmd/vms
```

## What was ported, and how

Read each Go file beside its Python twin: the names are the same and so are the decisions — `>=` on the revision, the slot claimed by CAS before an epoch is looked at, `TTL − margin` on a monotonic clock, the released-not-lapsed distinction, `promote` then the manifest line then the spool copy, closed buckets only. What changed is the shape around it:

| Python | Go | Why it matters |
|---|---|---|
| `dict` rows, `**fields` | `Camera` struct; `map[string]any` at the API edge only | The row is typed where the controller reasons about it; the JSON body stays a map where the operator writes it. |
| `Controller.write(path, mutate)` with `mutate -> None` | `Write(path, Mutate)` with `nil` = leave it; `panic(&ErrRow{})` for a refused row | The CAS loop is identical; a refusal (a deleted camera) became a typed error instead of `KeyError`. |
| `VmsWorker(Worker)`, `ClusterWorker(VmsWorker)` | struct embedding: `*p.Worker` inside `VmsWorker` inside `ClusterWorker` | Same layering; where Python overrode `place()` / `heartbeat_once()`, Go passes the override explicitly (`EnsurePlacedWith(c, placer)`, `Run(…, heartbeat)`). |
| `sqlite3` under the event index | a slice and a `seen` map, one mutex | The index is a cache either way; the standard library has no SQLite and a cache that admits to being one needs no file. |
| `yaml.safe_load` of the spec | `ParseYAML`, a subset: block and flow collections, scalars, comments | A spec that needs anchors or multi-line scalars has stopped being a spec; the parser is 150 lines and the VMS's file is embedded in the binary. |
| `threading.Thread × 4` racing `next_epoch` | four goroutines, `-race` clean | The Python test proves CAS under the GIL; the Go test proves it under real concurrency. |
| exceptions (`Conflict`, `Forbidden`, `Refused`) | `ErrConflict`, `ErrForbidden`, `*Refused` with `errors.Is` / `errors.As` | Every `except Exception: log` became an explicit `if err != nil` — most of the extra lines. |
| `json.dumps` of a heartbeat | `map[string]any` → `encoding/json` | Byte-compatible: a Go worker's heartbeat is read by the Python controller and the other way round (the М11 port checks it against the Python fixtures' shape). |

Not here, and not in the Python either: the GStreamer actuator and `archivesink` (in a Go worker they become a *client* of a C++ media process — М9 Lesson 7's per-frame rule survives cgo). `cmd/vms` runs both processes with the fake actuator: everything about slots, epochs, leases, placement, events and the console is real; nothing records.
