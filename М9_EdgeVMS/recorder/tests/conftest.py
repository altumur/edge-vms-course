"""Fakes for the tests that run in milliseconds on every commit.

Lesson 8, Step 6: the Lesson 6 tests need no database, no GStreamer and no
network. The rest need a Postgres container (DATABASE_URL) and a simulated
camera (tools/fake_camera.py) — still no appliance.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apphost.config import Settings  # noqa: E402


def cam(i: int, revision: int = 1, enabled: bool = True, **kw) -> dict:
    return {"id": i, "site_id": "hq", "name": f"cam{i}", "rtsp_url": f"rtsp://10.0.0.{i}/s",
            "cred_username": None, "cred_secret": None, "enabled": enabled,
            "retention_days": kw.get("retention_days", 30), "priority": kw.get("priority", 100),
            "revision": revision}


class FakeStore:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def desired(self) -> list[dict]:
        return self.rows


def settings(**overrides) -> Settings:
    env = {"ARCHIVE_DIR": "/tmp/recorder-test-archive", "DATABASE_URL": "postgresql://none"}
    env.update({k.upper(): str(v) for k, v in overrides.items()})
    old = dict(os.environ)
    os.environ.update(env)
    try:
        return Settings()
    finally:
        os.environ.clear()
        os.environ.update(old)


UTC = timezone.utc


class FakeFs:
    """A disk with a size. Files have bytes; usage is their sum over capacity."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self.files: dict[str, tuple[int, float]] = {}      # path -> (bytes, mtime)
        self.unlinked: list[str] = []

    def put(self, path: str, size: int, mtime: float = 0.0) -> None:
        self.files[path] = (size, mtime)

    def usage(self, path: str) -> float:
        return sum(b for b, _ in self.files.values()) / self.capacity

    def unlink(self, path: str) -> None:
        self.files.pop(path, None)
        self.unlinked.append(path)

    def walk_files(self, root: str):
        return [(p, m) for p, (_, m) in self.files.items() if p.startswith(root)]


class FakeDb:
    """Just enough of PgStore for retention.py: month partitions holding
    segment rows, cameras with retention_days and priority, an event log."""

    def __init__(self, cameras: dict[int, dict]):
        self.cameras = cameras                              # id -> {"retention_days", "priority"}
        self.parts: dict[str, dict] = {}                    # name -> {"parent","lo","hi","rows":[...]}
        self.events: list[tuple[str, int | None, dict]] = []
        self.dropped: list[str] = []
        self.log: list[str] = []                            # operation order, for the crash-order test

    def add_partition(self, parent: str, month: date) -> str:
        name = f"{parent}_{month:%Y_%m}"
        y, m = (month.year + 1, 1) if month.month == 12 else (month.year, month.month + 1)
        self.parts.setdefault(name, {"parent": parent, "lo": date(month.year, month.month, 1),
                                     "hi": date(y, m, 1), "rows": []})
        return name

    def add_segment(self, cid: int, start: datetime, path: str, size: int) -> None:
        for name, p in self.parts.items():
            if p["parent"] == "segments" and p["lo"] <= start.date() < p["hi"]:
                p["rows"].append({"camera_id": cid, "start": start, "path": path, "bytes": size})
                return
        raise AssertionError(f"no partition of relation segments found for row at {start}")

    def all_rows(self) -> list[dict]:
        return [r for p in self.parts.values() if p["parent"] == "segments" for r in p["rows"]]

    async def partitions(self, parent):
        return [(n, p["lo"], p["hi"]) for n, p in sorted(self.parts.items()) if p["parent"] == parent]

    async def ensure_partition(self, parent, month):
        return self.add_partition(parent, month)

    async def paths_in_partition(self, part):
        return [r["path"] for r in self.parts[part]["rows"]]

    async def drop_partition(self, parent, part):
        self.log.append(f"drop {part}")
        del self.parts[part]
        self.dropped.append(part)

    async def expire_rows(self, cid, older_than):
        out = []
        for p in self.parts.values():
            keep = []
            for r in p["rows"]:
                if r["camera_id"] == cid and r["start"] < older_than:
                    out.append(r["path"]); self.log.append(f"delete-row {r['path']}")
                else:
                    keep.append(r)
            p["rows"] = keep
        return out

    async def oldest_segments(self, limit, priority_first):
        rows = self.all_rows()
        key = (lambda r: (self.cameras[r["camera_id"]]["priority"], r["start"])) if priority_first \
            else (lambda r: r["start"])
        return sorted(rows, key=key)[:limit]

    async def delete_segment(self, path):
        self.log.append(f"delete-row {path}")
        for p in self.parts.values():
            p["rows"] = [r for r in p["rows"] if r["path"] != path]

    async def retention_days(self):
        return {cid: c["retention_days"] for cid, c in self.cameras.items()}

    async def indexed_paths_under(self, prefix):
        return {r["path"] for r in self.all_rows() if r["path"].startswith(prefix)}

    async def log_event(self, kind, camera_id=None, payload=None):
        self.events.append((kind, camera_id, payload or {}))


class LoggingFs(FakeFs):
    """Shares the FakeDb's operation log so a test can assert ORDER."""

    def __init__(self, capacity: int, db: FakeDb):
        super().__init__(capacity)
        self.db = db

    def unlink(self, path):
        self.db.log.append(f"unlink {path}")
        super().unlink(path)
