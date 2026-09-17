"""The one test that holds the two halves together.

Everything else in this package proves Python self-consistent, and `../vmsserver-go`'s 89 tests prove Go
self-consistent. Neither says a word about whether the two AGREE — and they meet nowhere but in the
store, so a disagreement would be silent: a renamed heartbeat field, a slot row written with a different
spelling, a manifest line with a float where the other side expects an int. Nothing would fail to
compile, no suite would go red, and the first sign of it would be a camera that is placed and never
recorded.

So this runs the real Go binary against the real Python controller over one directory, and asserts what
each half can only learn from the other:

  1. the Python controller places a camera on a worker it has never spoken to and can only SEE — the Go
     worker's heartbeat is the entire basis for the decision;
  2. the Go worker reads that placement out of its assignment row, takes the epoch, and says so in a
     heartbeat Python parses back into the console's read model;
  3. a segment the Go recorder promotes is read by Python's Manifest, with the same path grammar, the
     same line and the same numbers;
  4. a planned stop (SIGTERM) releases the slot ON PURPOSE, which is a different row from a lapse — the
     thing М10A Lesson 22's rolling upgrade depends on.

It needs a Go toolchain and skips without one (the authoring box has none; CI has both).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vms.archive import Manifest, Segment, segment_path  # noqa: E402
from vms.controller import VmsController  # noqa: E402
from w2cplatform.objects import FsObjectStore  # noqa: E402
from w2cplatform.contract import Slot  # noqa: E402
from w2cplatform.variables import FileVariables  # noqa: E402


def p_slot(vars_, name):
    """Go's row, through Python's own reader: the contract is the ROW, not either dataclass."""
    return Slot.from_items(name, vars_.get(f"vms/slots/{name}")[0])

HERE = os.path.dirname(os.path.abspath(__file__))
GO_SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "vmsserver-go")

pytestmark = pytest.mark.skipif(shutil.which("go") is None,
                                reason="no Go toolchain: the cross-language test needs one")


def _build(tmp: str) -> str:
    """One binary, built once — `go run` would recompile per process and the timings below are real."""
    out = os.path.join(tmp, "vms")
    subprocess.run(["go", "build", "-o", out, "./cmd/vms"], cwd=GO_SRC, check=True,
                   capture_output=True, timeout=300)
    return out


def _until(predicate, timeout=20.0, step=0.05):
    """Poll a CONDITION rather than sleep — the same discipline the drain route exists for."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(step)
    return None


def _start(binary, role, root, archive, spool, **env):
    e = dict(os.environ, PLATFORM_DIR=root, ARCHIVE=archive, SPOOL=spool,
             SERVER_NAME="srv-1", CAPACITY="50", **env)
    return subprocess.Popen([binary, role], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _stop(proc, sig=signal.SIGTERM):
    if proc.poll() is None:
        proc.send_signal(sig)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return proc.stdout.read() if proc.stdout else ""


def test_a_python_controller_places_a_camera_on_a_go_worker():
    root = tempfile.mkdtemp(prefix="cross-")
    archive, spool = os.path.join(root, "archive"), os.path.join(root, "spool")
    binary = _build(root)
    vars_ = FileVariables(os.path.join(root, "config"))
    objects = FsObjectStore(os.path.join(root, "objects"))
    ctl = VmsController(vars_, objects)
    ctl.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})

    started = time.time()
    w = _start(binary, "worker", root, archive, spool, WORKER_NAME="w-1")
    try:
        # 1. the slot, claimed by CAS — the first row either half writes, and Python reads it as its own
        slot = _until(lambda: vars_.get("vms/slots/w-1")[0])
        # the row Python's Slot.from_items expects, written by Go: the name is the KEY, the holder is the
        # instance, and `released` is the string "true"/"false" both sides agree to spell that way
        assert slot and slot["holder"] and slot["released"] == "false" and float(slot["until"]) > 0, slot
        assert p_slot(vars_, "w-1").holder == slot["holder"], "Python could not parse Go's slot row"
        hb = _until(lambda: objects.get("vms/w-1/heartbeat"))
        assert hb, "the Go worker never published a heartbeat Python could find"
        seen = _until(lambda: ctl.workers_seen(45.0).get("w-1"))
        assert seen is not None and seen.extra["server"] == "srv-1" and int(seen.extra["capacity"]) == 50

        # …and PROMPTLY, which is the part a suite on either side cannot check. Both loops heartbeat every
        # ten seconds; the question is whether the FIRST one waits for that tick. Python's did not, by an
        # accident of `time.monotonic()` counting from boot, and Go's did, because its monotonic counts
        # from process start — the same loop, and a restarted Go worker invisible to the controller, its
        # cameras unplaced, for ten seconds. Both now send it before entering the loop, deliberately.
        visible = time.time() - started
        assert visible < 5.0, f"the worker took {visible:.1f}s to become visible: the first heartbeat is late"

        # 2. the placement: the controller has never spoken to this process and never will. The heartbeat
        #    is the whole of what it knows, and it is enough to decide.
        placed = ctl.ensure_placed()
        assert [p.worker for p in placed] == ["w-1"], placed
        assert ctl.where(1) == "w-1"

        # 3. …and the worker finds it in its assignment row, takes the camera's epoch and says so
        running = _until(lambda: [s for s in (ctl.workers_seen(45.0).get("w-1").status or [])
                                  if str(s.get("id")) == "1" and s.get("phase") == "running"])
        assert running, "the Go worker never reported the camera the Python controller gave it"
        epoch, _ = vars_.get("vms/epoch/1")
        assert epoch == {"epoch": "1"}, epoch                      # the issuer's first number, written by Go
        assert running[0]["epoch"] == 1                            # an int stays an int across the seam
        assert running[0]["live_url"] == "rtsp://srv-1:8554/1"     # …and the fan-out a Python console proxies to

        # 4. schema and build travel in every heartbeat: what the upgrade route reads to decide
        raw = json.loads(objects.get("vms/w-1/heartbeat"))
        assert raw["schema"] == 1 and raw["build"] and raw["server"] == "srv-1"
    finally:
        log = _stop(w)

    # 5. a PLANNED stop: the slot is given back on purpose, which is not the same row as one that lapsed.
    #    Lesson 22's rolling upgrade is built on the difference.
    slot, _ = vars_.get("vms/slots/w-1")
    assert slot["released"] == "true", (slot, log)


def test_a_segment_the_go_recorder_promotes_is_read_by_python():
    """The archive tree is the SECOND contract the wide cut costs, and the only one that is a filesystem.

    A recorder writes the tree; the resource process — Python — repairs, retains and evacuates over it.
    They agree on three things: the path grammar rec/<unit>/e<epoch>/<stamp>.mp4, the manifest line, and
    the order (the file first, the line after). This checks all three with the real binary."""
    root = tempfile.mkdtemp(prefix="cross-")
    archive, spool = os.path.join(root, "archive"), os.path.join(root, "spool")
    binary = _build(root)
    vars_ = FileVariables(os.path.join(root, "config"))
    FsObjectStore(os.path.join(root, "objects"))

    # what a killed instance leaves behind: a closed segment in the spool, older than the grace
    start = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)
    pth = segment_path(spool, "1", 1, start)                  # Python's grammar, written by Python…
    os.makedirs(os.path.dirname(pth), exist_ok=True)
    with open(pth, "wb") as f:
        f.write(b"x" * 4096)
    old = time.time() - 120
    os.utime(pth, (old, old))

    r = _start(binary, "recorder", root, archive, spool, RECORDER_NAME="r-1")
    try:
        # …and promoted by Go on the way up, because a segment nobody indexed is footage nobody can find
        line = _until(lambda: Manifest(archive, "1").read() or None, timeout=20)
        assert line, "the Go recorder did not promote what the spool held"
        seg = line[0]
        assert isinstance(seg, Segment)
        assert seg.unit == "1" and seg.epoch == 1 and seg.path == "rec/1/e1/20260912T100000Z.mp4"
        assert seg.bytes == 4096 and seg.source == "live" and seg.start == start.timestamp()
        assert os.path.isfile(os.path.join(archive, seg.path))    # the file is where the line says
        assert not os.path.exists(pth)                            # and the spool copy went last
        assert Manifest(archive, "1").read() == [seg]             # idempotent: one line, not two
    finally:
        _stop(r)

    slot, _ = vars_.get("rec/slots/r-1")
    assert slot and slot["released"] == "true", slot
