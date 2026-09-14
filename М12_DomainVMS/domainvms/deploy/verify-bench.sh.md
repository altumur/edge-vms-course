# verify-bench.sh — М12 on a real bench: federation, the forwarded read, the agent's ACL, the signer's placement, the cold-start order, and the console with a cluster gone

**Role.** What the 41 tests cannot prove without Nomad, scripted. Needs federated regions (`federation.hcl`), the signer, agent, console and gateway jobs registered, and a management token in the environment (`NOMAD_TOKEN`). Run as `deploy/verify-bench.sh north south` — the domain region first. `set -u` (unset variables are errors) but not `-e`: each step decides for itself; an explicit `exit 1` marks a failed check, and a step that only prints is informational. Exit 0 otherwise. Compare М11's `verify-bench.sh` (`../../../М11_ClusterVMS/clustervms/deploy/verify-bench.sh.md`), which this continues.

## Environment
- `NOMAD_TOKEN` — implicitly, a management token for the `nomad` CLI (needed to create the client token in step 3 and to read across regions).
- `$1` = `DOMAIN`, `$2` = `OTHER` — required (`${1:?…}`).

## Step by step

### 1. two regions, one gossip pool
`nomad server members` must list servers from both regions; otherwise "regions not federated", exit 1. This is `federation.hcl`'s `retry_join` across regions having worked.

### 2. a Variable in the other region read from the domain region
`nomad var list -region "$OTHER" vms/ | head -5` — issued against the domain region's API with `-region`, so the request is forwarded: the federated read `DomainAgent` and the domain console depend on. Informational (no exit).

### 3. the agent may write `domain/*` and nothing else
Creates a client token carrying only the `domain-agent` policy (`agent-policy.hcl`) in the other region, extracts its `SecretID` with an inline Python `json.load`, then with that token: `nomad var put -force domain/keys current=probe` must succeed ("domain/keys: write ok"); `nomad var put -force vms/cameras/999 name=x` must fail — success is `FAIL: the agent could write vms/cameras/999`, exit 1; failure prints "403 (correct)". The same two assertions as `test_nothing_about_a_user_reaches_a_worker_only_trust_does`, against real ACLs. (The probe overwrites `domain/keys` in that region with `current=probe`; the agent's next sync restores it.)

### 4. the domain cluster is a stated decision, visible in the directory
`nomad var get -region "$DOMAIN" domain/signer` must exist ("signer keys in north's raft"); the same in `$OTHER` must *not* exist — if it does, `FAIL: signer keys in south`, exit 1. `signer.nomad.hcl`'s `region = "north"` made real.

### 5. the cold start order: signer before any certificate
`nomad job status domain-signer` must show `running`; otherwise exit 1. The order the `Signer` docstring names: Nomad up → signer scheduled → certificates issued → workers heartbeat.

### 6. the console lists both clusters, and says when one is unreachable
`curl http://console.$DOMAIN:8443/api/cameras?size=1` and print `clusters` and `complete` from the JSON. Then the instruction to the operator: drain the whole of the other region (or pull its uplink) and re-run the curl — expect `south: unreachable`, `complete: false`, rows still listed. This is `test_unreachable_cluster_keeps_last_known_rows_and_says_so` with a real link.

## Notes
- The script never registers the jobs or applies the policies; that is assumed done (М11's bench script shows the `nomad acl policy apply -job …` form).
- Step 6 depends on the console's read model filling, which needs a listing-capable object store in `CLUSTERS` (see `console.nomad.hcl.md`).
- Nothing here exercises the forwarded write (`PUT /api/cameras/<n>` to the owning cluster), which `console.main` leaves as a 501 stub; the README lists it among what needs a bench, but the script does not check it.
