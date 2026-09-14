#!/usr/bin/env python3
"""Commissioning, by hand — the temporary secrets Lesson 5 counts.

    python3 -m tools.provision key                      # generate the column key (once)
    python3 -m tools.provision operator admin           # prompts for a password
    python3 -m tools.provision camera --name lobby --url rtsp://10.0.0.41/stream1 \
            --site hq --user admin                       # prompts for the camera password
    python3 -m tools.provision migrate                  # apply migrations without starting the Worker

Everything here is superseded in М11/М12 (key delivery, the operator account);
none of it is the product's design, all of it is what a first recorder needs.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.worker import MIGRATIONS          # noqa: E402
from worker.config import Settings             # noqa: E402
from worker.secrets import ColumnKey           # noqa: E402
from worker.store import PgStore               # noqa: E402


async def _store(s: Settings) -> PgStore:
    return await PgStore.connect(s.database_url)


async def cmd_key(s: Settings, a) -> None:
    os.makedirs(os.path.dirname(s.column_key_file), exist_ok=True)
    ColumnKey.generate(s.column_key_file)
    print(f"column key written to {s.column_key_file} (mode 0600). "
          "This key is on the data partition and travels with every backup: named as a debt in Lesson 5.")


async def cmd_migrate(s: Settings, a) -> None:
    st = await _store(s)
    try:
        print("ok" if await st.migrate(MIGRATIONS) else "FAILED (previous schema intact)")
    finally:
        await st.close()


async def cmd_operator(s: Settings, a) -> None:
    from console.auth import hash_password
    pw = a.password or getpass.getpass("password: ")
    st = await _store(s)
    try:
        oid = await st.create_operator(a.username, hash_password(pw))
        print(f"operator {a.username} id={oid}. One account, all capabilities, no policy — "
              "the course's fourth temporary secret (Lesson 9).")
    finally:
        await st.close()


async def cmd_camera(s: Settings, a) -> None:
    key = ColumnKey.load(s.column_key_file) if a.user else None
    secret = (a.password or getpass.getpass("camera password: ")) if a.user else None
    if "@" in a.url.split("//", 1)[-1].split("/", 1)[0]:
        sys.exit("refusing: rtsp_url carries credentials inline. Use --user and the prompt.")
    st = await _store(s)
    try:
        if a.site:
            await st.upsert_site(a.site, a.site)
        cid = await st.create_camera(
            {"site_id": a.site, "name": a.name, "rtsp_url": a.url, "cred_username": a.user,
             "cred_secret": secret, "enabled": not a.disabled, "retention_days": a.retention,
             "priority": a.priority},
            encrypt=lambda sec, cid: key.encrypt(sec, cid))
        print(f"camera id={cid}. INSERT INTO cameras now causes a recording.")
    finally:
        await st.close()


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("key")
    sub.add_parser("migrate")
    op = sub.add_parser("operator"); op.add_argument("username"); op.add_argument("--password")
    cam = sub.add_parser("camera")
    cam.add_argument("--name", required=True); cam.add_argument("--url", required=True)
    cam.add_argument("--site"); cam.add_argument("--user"); cam.add_argument("--password")
    cam.add_argument("--retention", type=int, default=30); cam.add_argument("--priority", type=int, default=100)
    cam.add_argument("--disabled", action="store_true")
    a = p.parse_args()
    asyncio.run({"key": cmd_key, "migrate": cmd_migrate, "operator": cmd_operator, "camera": cmd_camera}[a.cmd](Settings(), a))


if __name__ == "__main__":
    main()
