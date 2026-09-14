# cloud.py — Lesson 8: the bandwidth-and-cost arithmetic before the demo, and М11's worker job rendered three ways to prove it is one artifact

**Role in the module.** Lesson 8, "a cluster you rent, and a worker that does not know where it is". The arithmetic first: fifty cameras at 4 Mbit/s is 200 Mbit/s sustained upstream and ~2 TB a day; most sites cannot buy that, so recording stays at the edge and operation moves to the cloud — MIXED is the shape a real deployment takes. Then the artifact: a worker deployed three ways — local server, rented instance, split — is the SAME job; if it is not, the lesson found a bug in М9 or М11. Reads М11's `deploy/vmsworker.nomad.hcl` as it is (`../../../М11_ClusterVMS/clustervms/deploy/vmsworker.nomad.hcl.md`); the README's "nothing was written downward for this module". Used by the Lesson 8 test only.

## Functions

### `bandwidth_mbit(cameras, mbit_per_camera)` — sustained upstream, Mbit/s.
### `tb_per_day(cameras, mbit_per_camera)` — bits/s → bytes → a day → TB (decimal): 50 × 4 Mbit/s = 2.16 TB/day.
### `storage_tb(cameras, mbit_per_camera, retention_days)` — the day figure × retention.

## `class Prices` (dataclass)
Order-of-magnitude list prices: `hot_object_per_tb_month = 22.0` (public-cloud hot object storage), `disk_per_tb_once = 25.0` (a disk), `disk_life_months = 60` (amortisation).

### `monthly_cost(tb, where, prices=Prices())`
`cloud`: TB × hot price per month; otherwise (edge): TB × disk price ÷ life in months.

## `class Recommendation` (dataclass)
- `shape: str` — `edge` | `cloud` | `mixed`; `upstream_mbit`, `tb_per_day`, `reason` — the sentence with the numbers.

### `recommend(cameras, mbit_per_camera, uplink_mbit, retention_days=30, prices=Prices())`
Three rules in order: upstream above 70 % of the uplink → `mixed` ("recording stays at the edge (~$N/mo in disks), operation moves to the cloud"); eight cameras or fewer → `cloud` ("operationally simpler, not cheaper … the camera is the buffer on an uplink outage"); else `edge` ("the uplink could carry it, but X TB costs ~$C/mo hot against ~$E/mo on disks; a datasheet implying otherwise loses money per camera"). `test_fifty_cameras_at_four_megabit`: 50 cameras on a 100 Mbit/s uplink → mixed; 6 cameras on a gigabit → cloud; 50 on 10 Gbit/s → edge.

### `_worker_jobspec() -> str`
М11's worker job, as written. The same three-candidate search as `domain/__init__.py` — `domainvms/clustervms/deploy/…`, `<course>/М11_ClusterVMS/clustervms/deploy/…`, `$CLUSTERVMS_PATH/deploy/…` — returning the file's text; `FileNotFoundError` naming the file if none.

### `render_three_ways(job="vmsworker") -> dict[str, str]`
The base jobspec with exactly two lines rewritten: the `datacenters = […]` line and the `OBJECTS = "…"` env line (matched by their stripped prefix, replaced with fixed indentation matching М11's file). Three variants: `local` (`room-a`, MinIO in the room over `s3+http://`), `rented` (`cloud-eu-1`, a provider's S3 over `s3+https://`), `split` (the same as `local` — the recorder in a split site stays at the edge; what moves to the cloud is not the worker). "What must never differ: everything about the worker."

### `worker_stanza(jobspec) -> str`
The part of a jobspec that *is* the worker: from `group ` to the end, with any line containing `OBJECTS` or `datacenters` replaced by `<placement>`. `test_a_worker_deployed_three_ways_is_the_same_artifact`: the three renderings differ as whole files and are identical from `group` down; `lost_after = "45s"` is still in the rented one and no `EPOCH` is set anywhere (the worker takes its epochs from raft, never from a file).

## Notes
- The `split` rendering is byte-identical to `local`; the test's three-way equality is therefore a two-way check plus an identity. It still says what the lesson wants — the split site's worker is the local worker.
- The rewrite is by line prefix; if М11's jobspec ever moved `datacenters` inside another block or renamed `OBJECTS`, the rendering would silently leave the original line in place and the `worker_stanza` mask would still hide it.
