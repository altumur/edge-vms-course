# provision.py — commissioning by hand: the column key, the operator account, a camera, and migrations

**Role in the module.** The temporary secrets Lesson 5 counts, provisioned from a shell: `python3 -m tools.provision key | migrate | operator <name> | camera --name … --url …`. Everything here is superseded in М11/М12 (key delivery, the operator account); none of it is the product's design, all of it is what a first recorder needs. Reads the same `Settings` as the Worker, so `DATABASE_URL`, `COLUMN_KEY_FILE` and friends come from the environment (README step 3 exports them). Depends on `worker.worker.MIGRATIONS`, `worker.config`, `worker.secrets`, `worker.store`, and `console.auth` for hashing. Inserts `recorder/` into `sys.path` so it runs from anywhere.

## Functions
### `_store(s) -> PgStore` — `PgStore.connect(s.database_url)`.

### `cmd_key(s, a)`
Creates the directory of `column_key_file` and `ColumnKey.generate` (refuses an existing file: `O_EXCL`). Prints the path and the debt: the key is on the data partition and travels with every backup (Lesson 5).

### `cmd_migrate(s, a)`
`PgStore.migrate(MIGRATIONS)` without starting the Worker; prints `ok` or `FAILED (previous schema intact)`. The Worker runs the same migrations at startup, so this is for commissioning before the first start.

### `cmd_operator(s, a)`
`--password` or a `getpass` prompt; `store.create_operator(username, hash_password(pw))`. Prints the id and the reminder: one account, all capabilities, no policy — the fourth temporary secret (Lesson 9).

### `cmd_camera(s, a)`
Loads the column key only if `--user` was given; the camera password comes from `--password` or a prompt, only with `--user`. Refuses a `--url` whose host part contains `@` ("rtsp_url carries credentials inline. Use --user and the prompt") — the same rule the console enforces. Upserts the site if `--site`, then `store.create_camera` with `site_id, name, rtsp_url, cred_username, cred_secret, enabled (not --disabled), retention_days (--retention, 30), priority (--priority, 100)` and `encrypt = key.encrypt`. Prints the id: "INSERT INTO cameras now causes a recording."

### `main()`
argparse with four subcommands: `key`, `migrate`, `operator <username> [--password]`, `camera --name --url [--site] [--user] [--password] [--retention N] [--priority N] [--disabled]`; dispatches to the `cmd_*` coroutine with a fresh `Settings()` via `asyncio.run`.

## Notes
- With `--user` but no key file, `ColumnKey.load` raises `FileNotFoundError` before any database call — run `key` first, as the README's order says.
- The `encrypt` lambda dereferences `key` only when a secret exists, so a camera without `--user` works on a box that has no key.
