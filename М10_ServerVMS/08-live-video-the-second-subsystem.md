# Lesson 8 — Live Video: the Second Subsystem

**Module:** ServerVMS — the platform's shape on one server (Module 10)
**You will build:** live video as a subsystem — `live.subsystem.yaml`, whose unit is a camera's fan-out and whose capacity is viewers; the gateway as a worker of it, subscribing to the worker's fan-out (`live_url`) once per camera and fanning out over WebRTC; the console's WHEP door; units created by the first viewer and deleted after the last; failover as the worker's.
**Time:** ~120 minutes.

## Why this lesson exists

A person in front of a wall wants *now*, and "now" is per-frame, per-viewer work — DTLS handshakes, ICE, RTP pacing, a browser that stalls — none of which may run in the process that holds the camera. So live view is a fifth process, and the question the platform asks of any process is the one Lesson 6, Step 6 asks of anything that wants a controller: *what is its unit, and what is its capacity counted in?* Answering it is what makes live video a subsystem rather than a feature: the unit is a camera's fan-out, the capacity is viewers, and everything else — the controller, the console, placement, failover, scaling — is the platform's, already written.

> **What you can verify without hardware.** `tests/test_lesson8_live.py`: the first viewer creating the unit and the controller placing it; fifty viewers on one subscription with the worker's heartbeat unchanged; the grace period and the gateway deleting its own unit; a dead gateway's fan-outs moved to the survivor and viewers reconnecting; placement by label; the two subsystems sharing the platform. The media path (`gstvms/webrtc.py`) needs `webrtcbin` and a browser: bench work.

## Prerequisites

- **Lesson 6** — `SpecController`, placement by capacity and label, `retire` and `redistribute`.
- **Lesson 7** — the console's token, the idempotency key, the mount.
- **Lesson 4** — the worker's heartbeat: where the gateway learns a camera's `live_url`; **Lesson 5** — the recorder, the first subscriber to it.
- **М12 Lesson 3** (read ahead) — *who serves browsers: never a worker*; the gateway contract this lesson builds.

## Learning objectives

1. Write a subsystem whose unit is demanded rather than configured and whose capacity is not cameras.
2. Build a worker that subscribes once per unit and fans out to N clients, and prove the worker holding the camera never learns a viewer exists.
3. Signal WebRTC over WHEP through the console, with the console carrying no media.
4. Let the worker delete its own idle units, and the controller take the placement back.
5. Show a gateway's death handled as a worker's: a lapsed slot, a released slot, redistribution, viewers reconnecting.

---

## Step 1 — The unit is a fan-out, the capacity is viewers

The page plays what the resource holds, one segment at a time. A person in front of a wall wants *now*, and "now" is per-frame, per-viewer work — DTLS handshakes, ICE, RTP pacing, a browser that stalls — none of which may run in the process that holds the camera. So live view is a fifth process, and the question the platform asks of any process is the same: *what is its unit, and what is its capacity counted in?* The answer is what makes it a subsystem rather than a feature: **the unit is a camera's fan-out** — *camera 7 is being watched, from gateway g-2* — and **capacity is viewers**. `vms/live.subsystem.yaml` is that description, ten lines like the VMS's: rows `streams`, named by the camera (`id: cam`), a `labels` field for where the viewers are (`public-address`), a `grace` in seconds, capacity and headroom from the heartbeat, `labels-subset`, the snapshot. The controller is `SpecController` run from it (`python3 -m vms livecontroller` — no code of its own); the console for it is `SpecConsole` over the same file.

## Step 2 — The gateway is a worker

**The gateway is a worker.** `vms/liveworker.py`'s `LiveWorker` extends the platform's `Worker`: a slot claimed by CAS (`g-1`), an assignment read from `live/workers/g-1`, an epoch taken per fan-out, a heartbeat with `capacity`, `headroom` (viewers it could still take — what the autoscaler moves `N` on), its `url`, and a status line per stream. Its reconcile pass makes its subscriptions equal its assignment: for each fan-out it is assigned, it finds where the camera's stream is — the VMS worker's heartbeat carries `live_url` per running camera, `rtsp://<server>:8554/<cam>` (Lesson 4, Step 3b) — and subscribes **once**, exactly as the recorder did in Lesson 5. Fifty browsers are fifty `webrtcbin`s hung off one `tee` inside the gateway, not fifty subscriptions; `test_fifty_viewers_one_subscription_and_the_worker_unchanged` counts one, and compares the VMS worker's heartbeat object byte for byte before and after: the worker never learned a viewer exists. The worker's side is the fan-out itself — `tee ! queue leaky=downstream ! rtph264pay ! udpsink` into the RTSP server — the same bytes the recorder gets, no second encode, and a stalled subscriber loses packets at the leaky queue rather than pushing back on the camera's pipeline. That is the leaky-queue rule by construction, and it is what lets the gateway run on a different server from the worker: the first draft's loopback `udpsink` could not.

## Step 3 — Demand creates the unit; demand deletes it

**Demand creates the unit; demand deletes it.** Nobody configures a fan-out. The page's *Live* button makes a WebRTC offer and `POST`s it to the console's `/whep/<cam>` (WHEP: one HTTP round trip, offer in, answer out, `Location` for the hang-up). On the first viewer the console — whose token may write the operator's rows of the subsystems it fronts, `live/streams/*` included — creates `live/streams/7` and answers **503, retry in 2 s**, because placement is the live controller's pass and the console's token cannot place (`test_the_first_viewer_creates_the_stream_and_the_controller_places_it` makes it try). The controller places the fan-out on the gateway with the most viewer headroom that reaches the stream's labels; the gateway subscribes; the browser's retry is proxied to that gateway, whose SDP answer comes back with `Location: /whep/session/<id>?gateway=g-1`. From then on RTP flows gateway → browser and the console carries nothing. When the last viewer hangs up the gateway keeps the subscription for `grace` seconds (a returning viewer costs nothing) and then **deletes the unit itself** — a worker deleting a row, the one such crossing in the course, allowed because the row exists only for the audience — and the controller's next pass takes the placement back. A viewer who comes back recreates the unit under its name, one revision on.

## Step 4 — Failover is the worker's

**Failover is the worker's.** A gateway dies: its slot lapses, its viewers are gone with it, and the controller moves nothing on its own — a crash is left alone, as for a VMS worker — until the slot is released (the operator's `retire`, or Nomad's orderly stop); then `redistribute()` moves the fan-outs to the survivor and the page's next offer lands there. `test_a_dead_gateway_loses_its_fan_outs_to_the_survivor_and_viewers_reconnect` ends by checking the VMS worker is still holding camera 1 and never noticed.

```
POST /whep/1  (an SDP offer)     -> 503 {retry_after: 2}          live/streams/1 created; the console cannot place
                                    livecontroller: place(1) -> g-1 (most free capacity (100) among 1 worker(s); on srv-1)
                                    g-1: subscribe udp://srv-1:20001, epoch 1
POST /whep/1                     -> 201 application/sdp, Location: /whep/session/<id>?gateway=g-1
GET  /live/1                     -> {gateway: g-1, status: {phase: live, sessions: 1, port: 20001}}
× 50                             -> live_sessions 50 · live_headroom 50 · one subscription; vms/w-1/heartbeat unchanged
DELETE /whep/session/<id>?gateway=g-1  -> 200; 30 s later the gateway deletes live/streams/1; the controller unplaces it
POST /whep/1?labels=public-address     -> only a gateway on a server labelled public-address is eligible
```

## Step 5 — What is real, and what is bench

What is real and what is a stand-in: signalling, placement, the fan-out arithmetic and the failover are tested here with `FakePeer`, which answers any offer with a minimal SDP. `gstvms/webrtc.py` is the media path for a box — one `rtspsrc ! rtph264depay ! h264parse ! rtph264pay ! tee` per camera, one `queue ! webrtcbin` per viewer, WHEP answered with every ICE candidate so nothing trickles — and needs `webrtcbin`, libnice and a browser, so it is bench work like every GStreamer element in the course. Two things it does not do, stated: it does not transcode (a camera with B-frames or H.265 is *live unavailable: codec* in the status, and transcoding is a placement decision — a GPU label — not a silent default), and it has no TURN, so a viewer outside the LAN waits for М12, where the domain places a relay where a public address is and puts a token check on `/whep`.

**Deliverable:** press *Live* on the page and watch `live/streams/1` appear, get placed and answered; fifty tabs on one camera and one subscription on the gateway; the gateway killed and the next offer answered by the survivor after its slot is released; `test_lesson8_live.py` green.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `POST /whep/7` is 503 forever | The live controller is not running, or no gateway heartbeats. The console created the unit; placement is the controller's pass and the gateway's subscription follows it. |
| Two subscriptions for one camera | Two gateways hold the unit — a reassignment window, or a gateway that did not `release` on `_drop`. The epoch says which is current. |
| Fifty viewers, the worker's heartbeat changed | Something on the gateway called the worker. It must read `live_port` from the heartbeat and nothing else. |
| The stream row never disappears | Viewers hang up without `DELETE` on the session URL, so `idle_since` never starts; or the gateway's token lacks `live/streams/*`. |
| The browser shows nothing but the answer was 201 | The media path: B-frames or H.265 (no transcoding here), or a viewer outside the LAN with no TURN (М12). The status says `codec`. |
| A crashed gateway's fan-outs stay on it | Correct until its slot is released: a crash is left alone, an orderly stop or `retire` releases the slot and the controller redistributes. |

## Recap

- Live video is a subsystem because it has a movable worker, a placed unit and a capacity of its own — viewers.
- One subscription per camera whatever the audience; the worker's fan-out is leaky and fire-and-forget, and reachable from any server.
- The console is only the door: it creates the unit on the first offer, proxies to the gateway the controller chose, and carries no media.
- Units are demand-created and demand-deleted; the gateway deletes its own idle units after `grace`.
- A gateway fails over like any worker; `count = N` on viewer headroom is the autoscaler policy the VMS workers already have.

## Exercises

1. Set `grace` to zero. Trace a viewer who refreshes the page: how many units are created, how many placements, how many subscriptions?
2. Give the gateway a `count = 0` floor in М11. What does the first viewer of the day wait for, and who pays for the alternative?
3. Add transcoding as a `labels: [gpu]` requirement on a stream whose camera sends B-frames. Which controller places it, and what does the reason say?
4. A viewer outside the LAN: list what TURN needs (a public address, credentials, a placement constraint) and which of those is the domain's (М12).

## Where this is going

Three subsystems on the box read the same fan-out: the recorder keeps it, the gateway shows it to people, and [**Lesson 9**](09-detectors-the-third-subsystem.md) shows it to a model — a subsystem whose output is events, which is the case that proves events are a data shape and not a process.
