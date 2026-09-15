# recorder.py — the recorder as an allocation: М10's `RecWorker`, unchanged

**Role in the module.** Lessons 2 and 4. `ClusterRecorder(RecWorker)` is what `worker.py` is for the worker: М10's class by the name the lessons use, with the `env=` calling convention. Nomad hands it `NOMAD_ALLOC_INDEX` (→ slot `r-<i>`, claimed by CAS on `rec/slots/*`), `NOMAD_NODE_NAME` (the server whose archive it writes into), `NOMAD_META_labels`, `NOMAD_ALLOC_ID`, `CAPACITY`. It holds no camera: each recording's pipeline subscribes to `live_url` from the VMS worker's heartbeat and writes `rec/<cam>/e<epoch>/` on its server's archive; closed segments are promoted from the spool on every pass.

## Notes
- `tests/conftest.py::Cluster.recorder(index, server)` builds one over that server's `ArchiveResource`, as `worker()` builds a worker.
- The jobspec is `deploy/vmsrecorder.nomad.hcl`: the archive constraint, `spread`, `rec/policy {servers: distinct}` by default; the rec controller (`python3 -m cluster reccontroller`) moves its recordings when its server dies — `test_the_power_pull_moves_the_recording_and_leaves_the_footage_where_it_was_written`.
