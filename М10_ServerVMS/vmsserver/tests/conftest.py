"""One box in a temp directory: the platform's two stores, a spool and an
archive, a clock. No GStreamer — the actuator is the fake."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from w2cplatform.objects import FsObjectStore  # noqa: E402
from w2cplatform.variables import FileVariables  # noqa: E402


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class Box:
    """The platform on one box, plus the two directories the archive resource needs."""
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="vmsserver-")
        self.vars = FileVariables(os.path.join(self.root, "config"))
        self.objects = FsObjectStore(os.path.join(self.root, "objects"))
        self.spool = os.path.join(self.root, "spool")
        self.archive = os.path.join(self.root, "archive")
        self.clock, self.wall = Clock(), Clock(1_757_500_000.0)


def cam(i, revision=1, enabled=True, **kw):
    return {"id": i, "name": f"cam{i}", "source": f"driverpack://file/cam{i}.mp4", "enabled": enabled,
            "priority": 100, "revision": revision, **kw}


class FakeStore:
    def __init__(self, rows): self.rows = rows
    def desired(self): return self.rows
