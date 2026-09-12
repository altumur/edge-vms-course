"""The recorder's database, through asyncpg. Every SQL statement the AppHost and
the console run lives here, so the operator/controller column split
(Lesson 5, Step 3) is enforced in exactly one file:

  * `report()` and `set_condition()` write controller-owned columns and
    nothing else.
  * `create_camera()` / `update_camera()` accept operator-owned columns and
    nothing else. There is no code path that lets a client write `phase`,
    `observed_revision`, `last_seen` or `revision`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Awaitable, Callable

try:
    import asyncpg
except ImportError:                      # the millisecond test suite runs without it
    asyncpg = None                       # type: ignore[assignment]

log = logging.getLogger("apphost.store")

OPERATOR_COLUMNS = ("site_id", "name", "rtsp_url", "cred_username", "cred_secret",
                    "enabled", "retention_days", "priority")
DESIRED_SQL = ("SELECT id, site_id, name, rtsp_url, cred_username, cred_secret, "
               "enabled, retention_days, priority, revision FROM cameras ORDER BY id")


class Desired:
    """What the Reconciler reads. The AppHost refreshes `rows` from Postgres
    on every pass; the reconciler's own state stays in memory."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def desired(self) -> list[dict]:
        return self.rows


class PgStore:
    def __init__(self, pool: "asyncpg.Pool"):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, min_size: int = 1, max_size: int = 4) -> "PgStore":
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    # -- migrations (Lesson 5, Step 8) -----------------------------------
    async def migrate(self, migrations_dir: str) -> bool:
        """Idempotent, one transaction per file, never leaves the box
        unbootable: a failing migration is logged and the AppHost carries on
        with the schema it has. Expand-only within a release is a rule for
        the author of the .sql files, not something code can check."""
        async with self.pool.acquire() as conn:
            await conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version int PRIMARY KEY, name text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now())""")
            applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for fname in sorted(os.listdir(migrations_dir)):
                m = re.match(r"(\d+)_.*\.sql$", fname)
                if not m:
                    continue
                version = int(m.group(1))
                if version in applied:
                    continue
                with open(os.path.join(migrations_dir, fname), encoding="utf-8") as f:
                    sql = f.read()
                try:
                    async with conn.transaction():
                        await conn.execute(sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                            version, fname)
                    log.info("migration %s applied", fname)
                except Exception:                      # noqa: BLE001
                    log.exception("migration %s FAILED; continuing on the previous schema", fname)
                    return False
        return True

    # -- desired state ----------------------------------------------------
    async def fetch_desired(self) -> list[dict]:
        return [dict(r) for r in await self.pool.fetch(DESIRED_SQL)]

    async def listen(self, channel: str, callback: Callable[[str], Any]) -> "asyncpg.Connection":
        """LISTEN on a dedicated connection. The caller keeps polling
        regardless: a notification missed while disconnected is gone forever."""
        conn = await self.pool.acquire()
        await conn.add_listener(channel, lambda *a: callback(a[-1]))
        return conn

    # -- controller-owned writes (the only ones) --------------------------
    async def report(self, rows: list[tuple[int, int, str]]) -> None:
        """rows: (camera_id, observed_revision, phase)."""
        if rows:
            await self.pool.executemany(
                "UPDATE cameras SET observed_revision=$2, phase=$3, last_seen=now() WHERE id=$1",
                rows)

    async def set_condition(self, camera_id: int, condition: str, status: bool,
                            reason: str | None = None) -> None:
        # `since` moves only when the status flips: "storage unavailable since
        # 14:02" is the sentence that turns a ticket into a fix.
        await self.pool.execute("""
            INSERT INTO camera_conditions (camera_id, condition, status, reason)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (camera_id, condition) DO UPDATE
               SET reason = EXCLUDED.reason,
                   since  = CASE WHEN camera_conditions.status = EXCLUDED.status
                                 THEN camera_conditions.since ELSE now() END,
                   status = EXCLUDED.status""", camera_id, condition, status, reason)

    async def clear_conditions(self, camera_id: int) -> None:
        await self.pool.execute("DELETE FROM camera_conditions WHERE camera_id=$1", camera_id)

    async def index_segment(self, camera_id: int, start: datetime, end: datetime,
                            path: str, size: int, epoch: int) -> None:
        await self.pool.execute(
            "INSERT INTO segments (camera_id, span, path, bytes, epoch) "
            "VALUES ($1, tstzrange($2, $3, '[)'), $4, $5, $6)",
            camera_id, start, end, path, size, epoch)

    async def log_event(self, kind: str, camera_id: int | None = None,
                        payload: dict | None = None) -> None:
        await self.pool.execute(
            "INSERT INTO events (camera_id, kind, payload) VALUES ($1, $2, $3::jsonb)",
            camera_id, kind, json.dumps(payload or {}))

    # -- retention (Lesson 8, Step 3) ------------------------------------
    async def partitions(self, parent: str) -> list[tuple[str, date, date]]:
        """(name, lower, upper) for every month partition of `parent`."""
        rows = await self.pool.fetch("""
            SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) AS bound
            FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
            WHERE i.inhparent = $1::regclass ORDER BY c.relname""", parent)
        out = []
        for r in rows:
            m = re.search(r"FROM \('([\d-]+)[^']*'\) TO \('([\d-]+)", r["bound"])
            if m:
                out.append((r["relname"], date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))))
        return out

    async def ensure_partition(self, parent: str, month: date) -> str:
        return await self.pool.fetchval("SELECT ensure_month_partition($1::regclass, $2)", parent, month)

    async def paths_in_partition(self, part: str) -> list[str]:
        return [r["path"] for r in await self.pool.fetch(f'SELECT path FROM "{part}"')]

    async def drop_partition(self, parent: str, part: str) -> None:
        # Postgres has no DROP PARTITION. DETACH, then DROP TABLE: ~5 ms, and
        # the file is unlinked rather than left for VACUUM.
        async with self.pool.acquire() as conn:
            await conn.execute(f'ALTER TABLE {parent} DETACH PARTITION "{part}"')
            await conn.execute(f'DROP TABLE "{part}"')

    async def expire_rows(self, camera_id: int, older_than: datetime) -> list[str]:
        """Per-camera retention inside a live partition. Rows first, files
        after (Lesson 8: a crash then leaves orphans, not lies)."""
        rows = await self.pool.fetch(
            "DELETE FROM segments WHERE camera_id=$1 AND lower(span) < $2 RETURNING path",
            camera_id, older_than)
        return [r["path"] for r in rows]

    async def oldest_segments(self, limit: int, priority_first: bool) -> list[dict]:
        order = "c.priority ASC, lower(s.span) ASC" if priority_first else "lower(s.span) ASC"
        rows = await self.pool.fetch(f"""
            SELECT s.camera_id, s.path, s.bytes, lower(s.span) AS start
            FROM segments s JOIN cameras c ON c.id = s.camera_id
            ORDER BY {order} LIMIT $1""", limit)
        return [dict(r) for r in rows]

    async def delete_segment(self, path: str) -> None:
        await self.pool.execute("DELETE FROM segments WHERE path=$1", path)

    async def retention_days(self) -> dict[int, int]:
        return {r["id"]: r["retention_days"] for r in
                await self.pool.fetch("SELECT id, retention_days FROM cameras")}

    async def indexed_paths_under(self, prefix: str) -> set[str]:
        return {r["path"] for r in
                await self.pool.fetch("SELECT path FROM segments WHERE path LIKE $1", prefix + "%")}

    # -- console reads (Lesson 9) ----------------------------------------
    async def status(self) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM camera_status ORDER BY id")
        return [dict(r) for r in rows]

    async def conditions(self) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {}
        for r in await self.pool.fetch("SELECT * FROM camera_conditions ORDER BY camera_id, condition"):
            out.setdefault(r["camera_id"], []).append(dict(r))
        return out

    async def timeline(self, camera_id: int, start: datetime, end: datetime,
                       segment_seconds: int) -> list[dict]:
        # The && is the question; the lower(span) bounds are what lets Postgres
        # prune to one partition. Widened by one segment length, which is the
        # most a segment can extend past its own start.
        rows = await self.pool.fetch("""
            SELECT camera_id, lower(span) AS start, upper(span) AS "end", path, bytes, epoch
            FROM segments
            WHERE camera_id = $1
              AND span && tstzrange($2, $3, '[)')
              AND lower(span) >= $2 - make_interval(secs => $4)
              AND lower(span) <  $3
            ORDER BY lower(span)""", camera_id, start, end, float(segment_seconds))
        return [dict(r) for r in rows]

    async def events(self, kind: str | None, camera_id: int | None, limit: int) -> list[dict]:
        rows = await self.pool.fetch("""
            SELECT id, at, camera_id, kind, payload FROM events
            WHERE ($1::text IS NULL OR kind = $1) AND ($2::bigint IS NULL OR camera_id = $2)
            ORDER BY at DESC LIMIT $3""", kind, camera_id, limit)
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    async def operator(self, username: str) -> dict | None:
        r = await self.pool.fetchrow("SELECT id, pwhash FROM operators WHERE username=$1", username)
        return dict(r) if r else None

    async def create_operator(self, username: str, pwhash: str) -> int:
        return await self.pool.fetchval(
            "INSERT INTO operators (username, pwhash) VALUES ($1, $2) RETURNING id", username, pwhash)

    # -- operator-owned writes --------------------------------------------
    async def cameras(self) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, site_id, name, rtsp_url, cred_username, enabled, retention_days, "
            "priority, revision, observed_revision, phase, last_seen FROM cameras ORDER BY id")
        return [dict(r) for r in rows]

    async def create_camera(self, fields: dict, encrypt) -> int:
        fields = {k: v for k, v in fields.items() if k in OPERATOR_COLUMNS}
        secret = fields.pop("cred_secret", None)
        cols = list(fields)
        async with self.pool.acquire() as conn, conn.transaction():
            cid = await conn.fetchval(
                f"INSERT INTO cameras ({', '.join(cols)}) VALUES "
                f"({', '.join(f'${i+1}' for i in range(len(cols)))}) RETURNING id",
                *[fields[c] for c in cols])
            if secret:
                await conn.execute("UPDATE cameras SET cred_secret=$2 WHERE id=$1",
                                   cid, encrypt(secret, cid))
            return cid

    async def update_camera(self, camera_id: int, fields: dict, encrypt) -> bool:
        fields = {k: v for k, v in fields.items() if k in OPERATOR_COLUMNS}
        if "cred_secret" in fields:
            fields["cred_secret"] = encrypt(fields["cred_secret"], camera_id) if fields["cred_secret"] else None
        if not fields:
            return True
        cols = list(fields)
        sets = ", ".join(f"{c}=${i+2}" for i, c in enumerate(cols))
        res = await self.pool.execute(f"UPDATE cameras SET {sets} WHERE id=$1",
                                      camera_id, *[fields[c] for c in cols])
        return res.endswith("1")

    async def delete_camera(self, camera_id: int) -> bool:
        res = await self.pool.execute("DELETE FROM cameras WHERE id=$1", camera_id)
        return res.endswith("1")

    async def upsert_site(self, site_id: str, name: str) -> None:
        await self.pool.execute(
            "INSERT INTO sites (id, name) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name",
            site_id, name)
