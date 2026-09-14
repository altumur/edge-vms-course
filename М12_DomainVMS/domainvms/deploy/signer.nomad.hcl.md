# signer.nomad.hcl — the domain signer job: one job, two keys, in the DOMAIN CLUSTER only, kept off recorders by a constraint

**Role.** Lessons 4 and 7. The jobspec for `python3 -m domain.signer_service` (`../domain/signer_service.py.md`, `../domain/signer.py.md`); its token is `signer-policy.hcl`. The header comment: one job, two keys (the CA and the token issuer); `count = 1` is not exactly-one during a reschedule, and that is harmless here — same key, same signatures; the key lives in the Variable `domain/signer`, a software key on purpose, because a TPM would pin the job to one server and defeat the failover it just gained.

## Stanza by stanza

### `job "domain-signer"`
- `region = "north"` — the domain cluster; the comment: a stated decision, recorded where the directory can report it (`Cluster.is_domain_cluster`, and `verify-bench.sh` item 4 checks `domain/signer` exists in north's raft and not in south's). The only jobspec in М12 with a `region` line.
- `datacenters = ["*"]`, `type = "service"`.

### `group "signer"`
- `count = 1` — one signer; a reschedule may briefly run two, which is fine because both load the same `domain/signer`.
- `constraint { attribute = "${meta.role}"  operator = "!="  value = "recorder" }` — the comment: what the operator may say is a CONSTRAINT, never a server — not on a server carrying fifty cameras. `meta.role` is set in each client's `client.hcl`.
- `network { port "https" {} }` — a dynamic port; the service registration below is how it is found.
- `service { name = "domain-signer"  port = "https"  provider = "nomad" }` — registered in Nomad's catalogue so consoles and the registrar can find `/login`, `/revoke`, `/keys`. No health check.

### `task "signer"`
- `driver = "podman"`.
- `config { image = "vms/domainvms:latest"; args = ["python3", "-m", "domain.signer_service"] }`.
- `identity { env = true }` — `NOMAD_TOKEN`; the comment lists what it may write: `domain/signer`, `identity/*`, `domain/keys`, `domain/revoked` (the policy also adds licence and placement).
- `template { … destination = "local/signer.env"  env = true }`:
  - `DOMAIN_ID=acme` — the domain name: token `iss`, root CN `acme root g<n>`, the registrar id prefix, the licence's `domain`.
  - `NOMAD_ADDR=http://127.0.0.1:4646` — the local agent (north's raft).
  - `OBJECT_STORE_URL=http://minio.north:9000/domain` — the `domain` bucket for `identity/rev-N` and `users/<id>/prefs`; plain HTTP is enough here because the identity store only `put`s and `get`s (no listing).
  - `TOKEN_LIFETIME=900` — 15 minutes, the number Lesson 4 defends; not read by the code (`identity.TOKEN_LIFETIME` is the constant).
  - `IDENTITY_PUBLISH_FLOOR=60` — the minimum seconds between identity publishes: the stated RPO for users.
- `resources { cpu = 200  memory = 128 }` — signing is cheap; scrypt on login is the only real work.

## Notes
- The port is dynamic and the code binds `SIGNER_PORT` (default 8445), which is not set from `NOMAD_PORT_https` here; the registered port and the listening port would differ unless the template adds `SIGNER_PORT={{ env "NOMAD_PORT_https" }}`.
- The port is named `https` but the service speaks plain HTTP; a bench would put TLS in front (the LDevID/renewal flow the docstring defers needs mTLS).
- Same one-line `config`/`resources` style as the other jobspecs.
