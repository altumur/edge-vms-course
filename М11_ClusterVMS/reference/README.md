# М11 reference — what runs without a cluster

| File | Lesson | Runs |
|---|---|---|
| `variables.py` | 26, 28 | a faithful stand-in for Nomad Variables (`ModifyIndex`, `cas`, 409) and Variable Locks (opaque UUID, TTL) — nothing here is Nomad; everything here is what Nomad promises |
| `test_issuer.py` | 28 | the wrong answer (a lock's IDs have no order) and the right one (CAS: 200 epochs from four racing threads, all distinct); `python3 test_issuer.py` |
| `lease.py` | 28 | the lease state machine on a monotonic clock; partition and pause; what the margins buy; `python3 lease.py` |
| `fencing_demo.py` | 28 | the zombie writer with two real processes, `kill -STOP` / `kill -CONT`; `--no-fencing` shows the corruption; `python3 fencing_demo.py` |
| `rehydrate.py` | 27 | the six-step restore against fakes, the publication order, `unconfigured`, and the RPO measured over 1000 failovers; `python3 rehydrate.py` |
| `placement.py`, `test_placement.py` | 29 | placement by measured capacity under constraints; the stability property; the tidy rebalance that fails it; `python3 test_placement.py` |
| `node.nomad.hcl` | 26, 28 | М9's recorder as a Nomad job: two Podman tasks, the Variable template, workload identity, the `disconnect` block, a VLAN constraint |
| `server.hcl`, `client.hcl` | 25 | three servers, ACLs on, `data_dir` on `/data`, the Podman plugin, `meta.vlans` |
| `node-3-policy.hcl` | 26 | one writer per key: recorder 3 may write `nodes/node-3*` and read `nodes/*` — bind with `nomad acl policy apply -job node-3` |

The `.py` files ran; their output is what the lessons print. The `.hcl` files are written to the Nomad documentation for ≥ 1.8.0 and were not validated against a running agent here — `nomad job validate` on the Lesson 1 cluster is the first thing to do with them.
