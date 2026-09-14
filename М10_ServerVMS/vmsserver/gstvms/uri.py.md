# uri.py — `driverpack://file/<name>` resolved to a path under `MEDIA_DIR`, and every other URI refused — pure, no GStreamer

**Role in the module.** Lesson 2's one testable piece without a media stack. A camera's `source` field is a `driverpack://` URI: the real DriverPack opens vendor streams behind it (`driverpack://<vendor>/<host>`); this course ships only `driverpack://file/<name>`, a media file played in a loop. This module decides which is which and where the file is, and it deliberately imports nothing from GStreamer so the worker and the tests can resolve and refuse without one. Called by `driverpacksrc.DriverPackSrc.do_set_property` when the `uri` property is set. Tested by `test_lesson2_driverpacksrc.py::test_uri_resolution_and_the_refusal`.

## Functions

### `resolve(uri, media_dir=None) -> str`
`media_dir` defaults to `$MEDIA_DIR` (`/data/media`; `deploy/vms.env.example` sets it, and `vmsworker@.container` mounts it read-only). `urlsplit(uri)` and then three refusals, each a `ValueError`:
- scheme is not `driverpack` — "not a driverpack URI" (`rtsp://…` is refused: the VMS never opens a stream itself);
- netloc is not `file` — "driverpack://<vendor>/… names a vendor driver; this course ships only driverpack://file/<name>" (`driverpack://hikvision/10.0.0.7`; the test checks the words "vendor driver" in the message);
- the path, with its leading `/` stripped, is empty, contains `/`, or contains `..` — "bad media name" (`driverpack://file/../etc/passwd`, `driverpack://file/`).

Returns `<media_dir>/<name>`. The name is exactly one path segment, so the element can only ever open a file directly inside `MEDIA_DIR`.

## Notes
- The refusal is the whole point of the URI scheme: a camera row can carry a vendor URI today and be placed, edited and shown by the controller and console — none of which look at `source` — and only the worker's element says it cannot open it.
