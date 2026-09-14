# console.py — the one-box console: the platform's SpecConsole over the VMS spec, plus the two media routes only a VMS has

**Role in the module.** Lesson 5. Everything an operator's console needs to list, create, edit and delete cameras, show where they run, export metrics and take marks is `vmsplatform.console.SpecConsole` reading `vms.subsystem.yaml` (see `vmsplatform/console.py.md`); nothing in this file knows what a camera's fields are. What the VMS adds is the bytes: `GET /timeline/<id>?from&to` (segments and event buckets from this box's archive manifest, fenced ones marked) and `GET /segment/<path>` (one promoted segment, `Range` honoured, for the page's `<video>`). They are *registered* as the console's `extra` route function, not subclassed. The console is its own process (`python3 -m vms console`, `deploy/vmsconsole.container`) with its own token — `vms/cameras/*`, `vms/next_id`, `vms/retention/*`, `vms/idem/*` — and never placement. Depends on `archive.py` (`ArchiveResource`, `Manifest`) and `controller.py`.

## Module-level names
- `PAGE`, `send_file` — re-exported from `vmsplatform.console` (`noqa: F401`) for М11, which serves the same page and the same ranged file replies from a Nomad job.

## Functions

### `vms_routes(archive) -> extra`
Builds the `extra(handler, method, path, q)` function `SpecConsole` calls for every request its built-in routes do not claim. Returns `None` ("not ours") for anything but `GET`, or for any request when `archive` is `None` (a console without a resource on its server — the second console in `test_a_retry_that_lands_on_another_console_is_one_camera` is built that way). Otherwise:

- `GET /segment/<rel>` — `rel` is joined under `archive.root`. If it contains `..` or is not a regular file the reply is `404 {detail, error: "no such segment"}`. Otherwise `send_file(handler, path, "video/mp4")` writes the reply itself (whole, or `206` with `Content-Range` when the request carried `Range`) and `extra` returns `()` — the console's signal that the reply was already served. The test asks `bytes=10-19` of a 256-byte segment and gets exactly those bytes with `Content-Range: bytes 10-19/256`; a path that does not exist is 404.
- `GET /timeline/<id>?from&to` — `int` of the last path segment; `200` with `Manifest(archive.root, cid).timeline(from, to)` — `from` defaults to 0, `to` to `1e12`. Note `current_epoch` is not passed here, so on one box no span is marked `fenced` by this route; the `Manifest.timeline` fencing is exercised directly in `test_lesson3_archive.py`.
- anything else — `None`, so the console answers 404.

### `make_console(ctl, archive, wall=None) -> SpecConsole`
`SpecConsole(ctl, marks_root=archive.root if archive else None, wall=wall, extra=vms_routes(archive), media=archive is not None)`. With an archive: operator marks (`POST /marks`) go into the console's own event log under `console/<hostname:pid>/e1/` on this server's resource, and `/spec` reports `media: true` so the page draws a timeline and a player. Without one: no marks (503) and no media.

### `serve(ctl, archive, host="127.0.0.1", port=8080, wall=None) -> ThreadingHTTPServer`
`make_console(...).serve(host, port)`: the server in a daemon thread, returned so the caller can `shutdown()` it. `__main__.console` calls it with `$CONSOLE_HOST:$CONSOLE_PORT`; the tests with `port=0`.

## Notes
- `test_the_console_over_http` walks the whole surface through this `serve`: an idempotent POST is one camera; the console's `VmsController` cannot `place` (its token); `PUT {"worker": "w-9"}` is 400; `/cameras` shows `phase running`, `server srv-1`; `/where/1` agrees with the assignments; `/spec` says `rows cameras, media true`; `/metrics` has `vms_cameras_recording 1`; a mark lands in `console/<unit>/e1/…` and `subsystems_under(archive)` shows only `console` — never `vms/1/`, whose bucket has one writer; the page never says "camera" outside its HTML comment; then `/timeline/1`, a ranged `/segment/`, a 404, a PUT that bumps `revision` to 2, and a DELETE whose placement waits for `unplace_deleted`.
- The archive mount in `vmsconsole.container` is what lets `/segment/` serve bytes; `/data/spool` is mounted read-only there because the console reads and never records.
