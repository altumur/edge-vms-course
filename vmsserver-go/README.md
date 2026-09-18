# vmsserver-go — М10, whole, in Go

М10's platform and its first subsystem, whole, in Go, with the Python suite ported alongside unchanged in meaning. Both ports are complete and both are kept complete: the point is that the shape — a platform that knows nothing about cameras; a subsystem that is only a prefix, a controller and a worker — can be stated twice, in two languages, and come out the same.

That is a claim, so there is a test for it. `../vmsserver/tests/test_cross_go_worker.py` runs **this binary** against the **Python** controller over one store: the controller places a camera on a worker it can only see through a heartbeat, the worker reads the assignment out of the store, takes the epoch and says so in a heartbeat Python parses back; a segment this recorder promotes is read by Python's `Manifest`, same path grammar, same line, same numbers. A green suite here and a green suite there prove each side self-consistent and nothing at all about whether they agree — and the first time that test ran it found a ten-second divergence in the worker's own loop (see `vms/worker.go`, `Run`).

```
vmsserver-go/
  go.mod                       module vmsserver; standard library only
  w2cplatform/                 the platform — no import of vms/, and not the word "camera" (a test greps for it)
    variables.go               Variables: the interface (Get/Put/List/Delete) over an OPAQUE Index, FileVariables (one box, files, flock),
                               and the seam — OpenVars(url, writer, acl) + RegisterScheme, so a process is told CONFIG_URL and nothing else
    memvariables.go            the same contract in memory, registered as `memory://` — the second backend, and what the contract suite is FOR
    runtime.go                 what a runtime hands a process, under neutral names: <ROLE>_NAME, SLOT_INDEX, SERVER_NAME, LABELS, INSTANCE_ID
    objects.go                 ObjectStore: the interface and FsObjectStore
    epoch.go                   NextEpoch by CAS; Lease (Renew = read my epoch; MayWrite on a monotonic clock)
    contract.go                Subsystem, Assignment, Heartbeat, Slot; Controller (Write by CAS, WorkersSeen, Assign*, Slots, Retire);
                               Worker (ClaimSlot / RenewSlot / ReleaseSlot, TakeEpoch, RenewLeases, HeartbeatWith)
    events.go                  EventLog: buckets <sub>/<unit>/e<epoch>/<start>Z.events.jsonl; BucketsUnder, SubsystemsUnder
    resource.go                Resource: heartbeat, Retain by <sub>/retention[/<unit>], Mirror to PeersOf, Restore, Pass; Serve over HTTP
    eventdatabase.go           EventDatabase: Rebuild / Tail / Query / Forget over ONE resource's tree (own buckets and the mirror copies it holds);
                               MergedIndex — what a console has instead: every live resource's /events, merged
    unit.go                    what a row IS, from the spec's `unit` block: Row, FieldSpec (types, defaults, refusals), SubsystemSpec and its parse
    placement.go               where a row GOES, from the spec's `placement` block: SpecController — the one controller every subsystem runs — the pool,
                               the filters (constraint, spread_by), the preferences (near, home), the tie-break, redistribute, rebalance, EnsureHome
    yaml.go                    a YAML subset parser (block and flow, scalars, comments) so the spec needs no dependency
    heartbeats.go              reading heartbeats: the read model (every age) and the freshness filter a caller uses before it TALKS to a holder
    space.go                   the disk and the two marks on it: statvfs behind a seam, and the watermark's settings
    console.go  console.html   the console as data: SpecConsole over the same spec — the page (embedded, built from /spec), /<rows>, /where, /metrics with
                               the subsystem's prefix, /marks, the writes with the spec's refusals; a subsystem registers an Extra for its own routes;
                               and on the Mount itself, /drain (one machine is about to stop — is it safe yet) and /schema (the store's layout, and
                               whether every live process is new enough to raise it)
  vms/                         the VMS — a subsystem
    vms.subsystem.yaml         the VMS's controller, as a spec — embedded into the binary (go:embed); the same file the Python package reads
    config.go                  Spec and RecSpec (vms.subsystem.yaml, rec.subsystem.yaml); Camera, the typed view of a row — the worker's camera
                               or the recorder's recording; LiveURL / LiveShm, the worker's two fan-out branches
    reconciler.go              М9 Lesson 6's loop: Reconcile, Lost, Clear, Status
    archive.go                 two trees: rec/<unit>/ media (SegmentPath / Parse, Manifest — Read / Rewrite / Timeline, media only —
                               ArchiveResource: Promote, Repair, Retain) and vms/<cam>/ events (EventLogFor)
    archivepolicy.go           ArchivePolicy — the rec hook the recorder registers with the resource: repair, retention by rec/recordings/<unit>,
                               and Free(need), the watermark's one question
    space.go                   what the archive gives up first: evacuate what another server writes now (push, and delete only what its manifest
                               confirms), then cut above the floor from the deepest unit, then say the shortfall out loud
    worker.go                  VmsWorker: the slot, server, labels and capacity from the environment (a box or an allocation); the gate (an epoch
                               per start); holds the camera — live_url and live_shm in its status, events, no footage; LeasePass, Fence,
                               Observe, PumpOnce, Status, Headroom, Run; the hooks (Enrich, StatusExtra, BeforePass, AfterPump) the recorder fills;
                               and the DEVICE: one session per device however many channels are assigned, DeviceStatus, Playback, the `held` phase
    device.go                  Device and FakeDevice: N channels, an archive of its own, a session budget — what DriverPack connects to
    playback.go                the holder's second surface: GET /playback/<cam>?from&to and /devices — HTTP, because a browser must seek it
    backfill.go                the recorder closing OUR gaps from the device's archive: OurCoverage, Gaps, InWindow, Backfill(budget), Fetch
    recworker.go               RecWorker: VmsWorker over rec/recordings/*; Source from the VMS heartbeat — shm:// on the same server, rtsp://
                               elsewhere; Resubscribe when the camera's holder moves; PromoteClosed; rec_recordings_running, rec_segments_backfilled
    controller.go              VmsController: the SpecController in the VMS's words — CreateCamera / Cameras / Placement with int ids
    console.go                 the one-box console: the platform's SpecConsole over the VMS spec plus VmsRoutes — /timeline/<id> (ours, and the device's
                               in the holes), /segment/<path> with Range, /segment?cam (the holder's door, named not proxied), POST /backfill;
                               the recorder mounted at /rec/… (w2cplatform.Mount)
    resource.go                the resource process: NewVmsResource — the platform's Resource with the recorder's ArchivePolicy registered and an EventDatabase attached; ResourceRoutes
                               (/manifest, /segment), PUT /segment for footage changing hands, GET /space
  gstvms/uri.go                driverpack://file/<name> resolution — the pure part; the element itself is Python's (GStreamer)
  testbox/                     the fixture both Go suites share: FileVariables + FsObjectStore in a temp dir, a spool, an archive, two clocks
  cmd/vms/main.go              vms worker|controller|recorder|reccontroller|console|resource — the box's processes, with the fake actuator
  *_test.go                    89 tests, in the packages they test; -race clean — including variables_contract_test.go, the contract EVERY
                               backend must keep, runnable against another with CONTRACT_URL
```

```bash
go test ./...                    # 89 tests, ~600 ms
CONTRACT_URL=memory:// go test ./w2cplatform -run Contract              # the same contract, the other backend shipped here
CONTRACT_URL=nomad://127.0.0.1:4646 go test ./w2cplatform -run Contract   # …and one that is not
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
| `Index = str | int` | `type Index struct{v string; set bool}` — comparable, with `Absent` and `NoCAS` | Python states the opacity in a type alias; Go makes the compiler enforce it. `idx-1` does not compile, which is exactly the mistake a Kubernetes backend cannot survive. |
| `threading.Thread × 4` racing `next_epoch` | four goroutines, `-race` clean | The Python test proves CAS under the GIL; the Go test proves it under real concurrency. |
| exceptions (`Conflict`, `Forbidden`, `Refused`) | `ErrConflict`, `ErrForbidden`, `*Refused` with `errors.Is` / `errors.As` | Every `except Exception: log` became an explicit `if err != nil` — most of the extra lines. |
| `json.dumps` of a heartbeat | `map[string]any` → `encoding/json` | Byte-compatible, and no longer on trust: `../vmsserver/tests/test_cross_go_worker.py` has the Python controller read this binary's heartbeat and place a camera on it. |

Not here, and not in the Python either: the GStreamer actuator and `archivesink` (in a Go worker they become a *client* of a C++ media process — М9 Lesson 7's per-frame rule survives cgo). `cmd/vms` runs both processes with the fake actuator: everything about slots, epochs, leases, placement, events and the console is real; nothing records.

## The orchestrator is an install-time choice here too

Both ports keep the same three seams, because a seam in one language and not the other is not a seam:

- **the store is a URL** — `p.OpenVars(url, writer, acl)` with `p.RegisterScheme`; `cmd/vms` reads `CONFIG_URL` (default `file://<PLATFORM_DIR>/config`) in one helper, so the seven direct `NewFileVariables` constructions are down to the one in `testbox` — a test box, honestly the file backend;
- **the environment has neutral names** — `w2cplatform/runtime.go`: `<ROLE>_NAME`, `SLOT_INDEX`, `SERVER_NAME`, `LABELS`, `INSTANCE_ID`. No `NOMAD_*` is read anywhere in the loop; a jobspec or a manifest maps into these;
- **the index is opaque** — and in Go the compiler is the one enforcing it. `Index` is a comparable struct with no arithmetic on it, so the mistake Kubernetes' string `resourceVersion` punishes cannot be written.

`w2cplatform/variables_contract_test.go` is what turns that into a claim: ten tests in five clauses, driven against the file store, against an in-memory store whose version is `rv-<n>`, and — through `CONTRACT_URL` — against any backend a site actually has. The last of them drives the real CAS loops (`NextEpoch`, `ClaimSlot`, `ReleaseSlot`) over the non-numeric store, which is where an ordering or an increment would show.
