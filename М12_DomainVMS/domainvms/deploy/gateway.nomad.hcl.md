# gateway.nomad.hcl — the live gateway job: one subscription per camera to the owning worker's tee, N browsers out; placed by CONSTRAINT on a GPU and a public address, never by name

**Role.** Lesson 3. The jobspec for the process `../domain/gateway.py.md` models (WebRTC/WHEP, fMP4 and TURN are the real transport, which is why the image is not the Python one). Runs in EVERY cluster. The header comment is the design point: this is the one place a specific server comes back into the design — as a CONSTRAINT: a gateway that transcodes wants the GPU, one that faces the internet wants the public address; Nomad places it there because of them; nobody types its name. The gateway relays the viewer's token to the cluster's authoriser; it never authorises.

## Stanza by stanza

### `job "live-gateway"`
- `datacenters = ["*"]`, `type = "service"` — as the other jobs.

### `group "gateway"`
- `count = 1` — one gateway per cluster; the worker sees one subscriber per camera regardless of how many browsers watch.
- `constraint { attribute = "${meta.gpu}"  operator = "is_set" }` — only a client whose `client.hcl` declares `meta.gpu` (the transcoding hardware).
- `constraint { attribute = "${meta.public_addr}"  operator = "is_set" }` — only a client with a public address declared in its meta (the internet-facing one). Both constraints must hold: the same server, chosen by attributes.
- `network { port "whep" { static = 8444 }  port "turn" { static = 3478 } }` — WHEP (WebRTC-HTTP egress) on 8444 and TURN on the standard 3478, both fixed because browsers and firewalls are told them.
- `service { name = "live-gateway"; port = "whep"; provider = "nomad" }` — registered in Nomad's catalogue on the WHEP port, so the console can point a browser at it.

### `task "gateway"`
- `driver = "podman"`.
- `config { image = "vms/live-gateway:latest"  devices = ["/dev/dri:/dev/dri"] }` — a separate image (the transport is not Python); the DRI device passed through for the GPU — the comment: for transcoding what a browser cannot decode.
- `identity { env = true }` — its `NOMAD_TOKEN`, for reading the directory below and `domain/*` (keys, revocations, grants) for the cluster's authoriser. No policy file ships for it.
- `template { … destination = "local/gateway.env"  env = true }`:
  - `NOMAD_ADDR=http://127.0.0.1:4646` — the local agent.
  - `DIRECTORY=vms/workers/` — the comment: "where is camera N → which worker's tee to subscribe to (url from its heartbeat)": the assignments `vms/workers/<w>` (М10's assignment rows) say which worker holds a camera, and the worker's heartbeat carries the endpoint. This is `Gateway.where` and `Gateway.endpoint` as a Variable prefix and an object.
  - `PUBLIC_ADDR={{ env "meta.public_addr" }}` — the address to advertise in ICE candidates, taken from the node meta the constraint selected on.
  - `UPSTREAM_QUEUE=30` — frames; the comment: leaky — a stalled viewer loses frames, never stalls the worker. `LeakyQueue(maxsize=30)` in the model.
- `resources { cpu = 4000  memory = 2048 }` — 4 GHz and 2 GB: transcoding and many WebRTC sessions.

## Notes
- The gateway reads the cluster's *assignments* for the directory, not the domain's `where()`; within one cluster that is the authoritative answer, and it is a local read that survives the domain being down.
- Same one-line block style (`;` in `service`/`config`) as the other jobspecs — see `agent.nomad.hcl.md`.
