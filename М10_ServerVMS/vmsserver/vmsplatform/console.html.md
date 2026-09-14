# console.html — the console's one page, for any subsystem: built from /spec, no framework, no build step

**Role in the module.** Lesson 5. Served by `console.py` at `/`. The page reads `/spec` first — the subsystem's name, what its rows are called, how a unit is identified, which fields the operator owns and whether there is media — and builds the unit list, the add form and the edit form from that. Nothing on this page names a camera: the VMS is the spec that says `rows: cameras`, `media: true`; a counter is another spec and the same page (`test_the_second_subsystem_gets_a_console_for_free`, and `test_the_console_over_http` asserts the word "camera" does not appear outside the leading comment). With media the page adds the timeline and the player, using the routes the subsystem registered (`/timeline/<id>`, `/segment/<path>`) plus the platform's `/events?cam=`. Writes go through the console with a fresh `Idempotency-Key` per attempt so a retried click is one unit; refusals are shown as the server's `detail`.

## `<style>`
Light/dark palette via CSS variables (`--media` blue for recorded spans, `--events` amber for events-only spans and ticks, `--fenced` grey, `--live` green and `--stale` red for the status dot). A two-column grid: a 280 px `aside` and `main`. `#edit` and `#media` start hidden; `li.stale` is muted; `.span.fenced` is faded; `.span.playing` outlined.

## DOM regions
- `aside`
  - `#rows` — heading: the rows name from the spec plus a count.
  - `#units` — the unit list (`li` per unit: a phase dot, the label, `#id`, and on the right `worker · server · age s` or `unplaced`).
  - `#addbox` / `#addlabel` — a collapsible "Add" section containing form `#add` with `#addfields` (inputs generated from the spec), a submit button, `#addnote` (how the id is made) and `#adderr`.
  - `#status` — "N workers live · M <running gauge>" from `/metrics`.
- `main`
  - `#title` — the selected unit's label, or "Pick one".
  - `#meta` — phase · worker · server · epoch · placement reason · "last known state" when stale.
  - `#media` (only when `spec.media`) — a legend (recorded / events only / fenced), `#timeline` (spans and ticks), `#axis` with `#t0`, `#t1` and the `#last24` / `#all` window buttons, `#note` (the cluster's `note`, e.g. unreachable servers), `#player` (`<video controls>`), `#playing` caption.
  - form `#edit` — `#editfields` (inputs from the spec minus `enabled` and the id field), Save, `#toggle` (Enable/Disable, shown only if the spec has a bool `enabled`), `#del`, a reminder that worker/epoch/revision/phase are the controller's, and `#editerr`.

## State variables
- `spec` — the `/spec` reply; the page's only knowledge of the subsystem.
- `units` — the merged list from `/<rows>` (configured rows overlaid by heartbeat rows).
- `current` — the selected unit id.
- `window_` — `[from, to]` for the timeline, or `null` for everything.
- `spans` — the timeline's segments as normalised objects `{start, end, epoch, fenced, events, media, server}`.
- `queue` — indexes into `spans` still to play.

## Script functions
### `$`, `fmt`, `ROWS`, `label`, `hasEnabled`
Helpers: query selector; a UTC `YYYY-MM-DD HH:MM:SSZ` formatter; the rows path `/<spec.rows>`; a unit's display label (`name` if it has one, else singular rows name + id for numeric ids, else the id); whether the spec has a bool field named `enabled`.

### `loadSpec()`
`GET /spec`. Sets the document title, the rows heading, the add note (numbered by the controller vs. named by its `<id field>`), shows `#media` if `spec.media`, and renders the add form (every field but `enabled`) and the edit form (every field but `enabled` and the id field) with `input()`. Shows `#toggle` if `hasEnabled()`.

### `input(f, adding)`
One labelled input per spec field by type: checkbox for bool (checked by default value), number for int/float (`step="any"` for float; default as value when adding), text otherwise (placeholder `a, b` for lists, or the default when adding); `required` only on the add form.

### `loadUnits()`
`GET /<rows>`: starts from `configured` (each marked `phase: silent, worker_state: stale`) and overlays `rows` — the heartbeat's word wins over the row's. Sorts by id, rebuilds `#units` with class = phase, `stale` and `on`, and wires each `li` to `select`. Then fetches `/metrics` and writes the status line from `_workers_live` and `_<running gauge>`. Runs at load and every 10 s.

### `select(id)`
Marks the unit current, clears the play queue, sets `#title`, fetches `/where/<id>` for the placement reason, composes `#meta`, highlights the `li`, fills the edit form, and — if media — loads the timeline.

### `loadTimeline()`
`GET /timeline/<current>?from&to` (the subsystem's route). Accepts a plain list (one box) or `{segments, unreachable, note}` (a cluster), showing `note` in `#note`. Normalises spans; tries `GET /events?from&to&cam=<current>` and ignores a failure (503 without an index). Computes the visible range: the window if set, else from the earliest span/event (or now − 1 h) to the latest (or now). Draws each span as a `div.span` positioned by percentage — `events-only` when it has no media, `fenced` when fenced — with a tooltip naming times, epoch, server, event count and "not recorded"; clicking a span with media plays from it. Draws each event as a `div.tick` with a tooltip (time, kind, subsystem, note). Labels the axis.

### `key()`
A fresh UUID (or a random string) per request: the `Idempotency-Key`.

### `fields(form)`
Reads the form into a body by spec type: bools always sent as checked/unchecked; an empty input is omitted (the spec's default on create, unchanged on edit); lists split on commas and trimmed; ints/floats parsed.

### `send(method, path, body)`
`fetch` with `Content-Type: application/json` and a new `Idempotency-Key`; parses the reply; throws `detail || error || status` when not OK. Every write below uses it.

### Form and button handlers
- `#add` submit — `POST /<rows>` with the form fields; on success reset, reload, select the new id; on refusal show `refused: …` in `#adderr`.
- `#edit` submit — `PUT /<rows>/<current>` with the edit fields; reload and reselect; errors to `#editerr`.
- `#toggle` click — `PUT /<rows>/<current>` with `{enabled: !current.enabled}`.
- `#del` click — confirm (mentioning that footage stays on the resource until retention if media), then `DELETE /<rows>/<current>`; clears the selection and the timeline, reloads.
- `#player.onended` — `next()`.
- `#last24` click — window = last 24 h and reload the timeline; `#all` — no window.

### `fillEdit(c)`
Shows `#edit` and fills each spec field from the unit: checkbox for bool, comma-joined for list, value otherwise. Sets the toggle text to Enable/Disable from `c.enabled`.

### `segmentURL(s)`
`/segment/<media path>`, with `?server=` when the span names another server (a cluster).

### `play(i)`
Queue every span from index `i` onward that has media, then `next()`.

### `next()`
Pop the next index, outline that span as playing, write the caption (times, epoch, "from <server>"), set the video source to `segmentURL` and play. One segment at a time, the next when this one ends; gapless MSE playback of fragmented MP4 is the gateway's job (М12) — the console plays what the resource serves.

### Boot
`loadSpec().then(() => { loadUnits(); setInterval(loadUnits, 10000); })`.

## Routes the page uses
- `GET /spec`, `GET /<rows>`, `GET /metrics`, `GET /where/<id>` — the platform console.
- `POST /<rows>`, `PUT /<rows>/<id>`, `DELETE /<rows>/<id>` — the writes, always with an `Idempotency-Key`.
- `GET /timeline/<id>?from&to`, `GET /segment/<path>[?server=]` — only when `spec.media`; registered by the subsystem.
- `GET /events?from&to&cam=<id>` — the eventindex behind the console, tolerated when absent.

## Notes
- The page never sends `worker`, `epoch`, `revision`, `phase` or `id` — the inputs are generated only from `spec.fields`, and the server would refuse them anyway (`PLATFORM_FIELDS`).
- The list's colour is the heartbeat's phase (`running` green, `failed` red, anything else grey), and a stale worker's rows are muted with "last known state" in the meta line: the read model survives a controller outage and degrades honestly on a worker outage.
