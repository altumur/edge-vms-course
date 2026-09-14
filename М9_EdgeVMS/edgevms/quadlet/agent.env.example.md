# agent.env.example — the М8 agent's environment on the data partition: the first temporary secret

**Role.** Lesson 4. Template for `/data/config/agent.env`, provisioned by hand at commissioning with `chmod 600`, and loaded by `EnvironmentFile=` in both `quadlet/vms-agent.container` and `quadlet/spool-uploader.container`. It lives on `/data`, never in the image, so an OS update cannot replace it — and, as `rauc/build-bundle.sh --broken-config` notes, so that no bundle can break it. The course names it as its FIRST temporary secret: a static AWS key in a plaintext file on a device in a warehouse. М12 Lesson 8 replaces it with workload identity.

## Key by key
- `AWS_ACCESS_KEY_ID=AKIA...` / `AWS_SECRET_ACCESS_KEY=...` — the static credential the agent's `kvssink` and the uploader's `vms-upload-segment` use to reach Kinesis Video Streams. The values are placeholders; the file is an example.
- `AWS_DEFAULT_REGION=eu-west-1` — the KVS region.
- `KVS_STREAM_NAME=site-42-camera-1` — the stream the agent publishes to and the uploader re-publishes acknowledged segments into.

## Notes
- No key here is read by the М9 recorder; the recorder's environment is `worker.env.example`.
