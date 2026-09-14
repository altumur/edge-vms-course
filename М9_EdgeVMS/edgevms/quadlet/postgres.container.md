# postgres.container — the recorder's PostgreSQL, PGDATA on the data partition

**Role.** Lesson 5, Step 9. Quadlet unit installed at `/etc/containers/systemd/postgres.container`; Podman's generator turns it into `postgres.service`, which `quadlet/worker.container` `Requires=` and orders `After=`. Its one design point is in the header: PGDATA on `/data/pg`, never in a rootfs slot, because an A/B update that destroyed every camera row would boot perfectly and never roll back — the health check's ladder has no row for "the configuration vanished".

## Stanza by stanza
### `[Unit]`
- `Description=recorder database`.

### `[Container]`
- `Image=docker.io/library/postgres:16` — the version the README's store was verified against (PostgreSQL 16.13). The image is pulled into `/data/containers/storage` (`storage.conf`), so it survives updates too.
- `Volume=/data/pg:/var/lib/postgresql/data:z` — PGDATA on the data partition; `bench/build-disk.sh` creates `/data/pg`. `:z` relabels for SELinux shared use.
- `EnvironmentFile=/data/config/pg.env` — `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` for the image's first-run initialisation, hand-provisioned like the other secrets on `/data/config`. The matching `DATABASE_URL` is in `worker.env.example`.
- `PublishPort=127.0.0.1:5432:5432` — loopback only; the worker runs with `Network=host` and connects to `127.0.0.1:5432`. Nothing off the box can reach the database.
- `HealthCmd=pg_isready -U recorder` with `HealthInterval=5s` — Podman's health check, visible in `podman ps`; it does not gate dependants (see Notes).

### `[Service]`
- `Restart=always` — Postgres comes back whatever killed it.

### `[Install]`
- `WantedBy=multi-user.target` — applied by the generator; do not `systemctl enable`.

## Notes
- There is no `pg.env.example` beside this file; the README's bench recipe uses `podman run -e POSTGRES_USER=recorder -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=recorder` instead, which is the content the file needs.
- `Requires=`/`After=postgres.service` in the worker unit waits for the container to *start*, not for `pg_isready`; without `Notify=healthy` the worker's first `PgStore.connect` can race the database's startup. The worker unit's `Restart=always`, `RestartSec=2` is what absorbs that: a failed connect is a crash and a retry two seconds later.
