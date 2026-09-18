"""A field too big for a row: the bytes in the object store, the digest in the row.

Some units carry one opaque lump — a detector's per-pixel mask, a panel's
firmware, a model. It belongs to exactly one unit, the platform never reads
inside it, and it does not fit in a row: a Nomad Variable holds 64 KiB and a
per-pixel mask for 1920x1080 is 345 KB as base64.

Moving such configuration wholesale into the object store is the obvious fix
and the wrong one: an object has no CAS, no one writer and no `revision`, so
the reconciler never learns it changed. These tests are about the shape that
keeps the mechanism — the row holds a DIGEST, and everything else follows from
that one choice.
"""
import base64
import json

from w2cplatform.blobs import digest, is_digest
from w2cplatform.console import SpecConsole
from w2cplatform.objects import FsObjectStore
from vms.config import DET_SPEC
from w2cplatform.spec import Refused, SpecController, SubsystemSpec
from tests.conftest import Box, FakeStore

MASK = base64.b64encode(bytes(1920 * 1080 // 8))       # 345 600 bytes: the real thing, not a stand-in


def _det(box, objects=None):
    ctl = SpecController(DET_SPEC, box.vars, objects or box.objects, wall=box.wall)
    return ctl, SpecConsole(ctl)


def test_the_row_takes_a_digest_and_says_so_when_it_is_handed_the_bytes():
    """The obvious thing for a client to do is paste the lump into the row, and that
    is also what puts a row over the store's ceiling. The refusal names the route
    instead of just saying no."""
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    try:
        ctl.update("7-linecross", {"mask": MASK.decode()})
        raise AssertionError("the bytes went into the row")
    except Refused as e:
        assert "takes a digest, not the bytes" in str(e)
        assert "/units/<id>/mask" in str(e)                     # where to put them instead


def test_the_bytes_go_to_the_object_store_under_their_own_name():
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    d = ctl.put_blob(MASK)                                       # 1. the object
    row = ctl.update("7-linecross", {"mask": d})                 # 2. the row that names it
    assert is_digest(d) and d == digest(MASK)
    assert row["mask"] == d
    assert box.objects.get(f"det/blobs/{d}") == MASK
    assert ctl.blob(d) == MASK
    # the row stayed a row: the lump is not in it, by three orders of magnitude
    assert len(json.dumps(DET_SPEC.items(row)).encode()) < 500 < len(MASK)


def test_the_same_bytes_are_one_object_however_many_units_name_them():
    """Content addressed, so this is true without anyone arranging it — and it is
    also why writing a blob twice is safe: the second write writes the same bytes
    to the same key."""
    box = Box()
    ctl, _ = _det(box)
    for i in (7, 8, 9):
        ctl.create({"name": f"{i}-linecross", "cam": str(i), "kind": "linecross"})
        ctl.update(f"{i}-linecross", {"mask": ctl.put_blob(MASK)})
    assert len(box.objects.list("det/blobs/")) == 1
    assert len(ctl.blobs_referenced()) == 1


def test_changing_the_blob_moves_the_revision_and_the_reconciler_restarts():
    """The whole reason the digest is in the row. Nothing here is a new mechanism:
    the reconciler has always restarted a unit whose revision moved, and a different
    lump is a different digest is a different row.

    Put the bytes in the object store alone and this test is impossible to write —
    the object changes, the row does not, and the worker goes on with the old mask
    until something else restarts it."""
    from vms.reconciler import Reconciler
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    r1 = ctl.update("7-linecross", {"mask": ctl.put_blob(MASK)})

    calls = []
    rows = FakeStore([{"id": 1, "revision": r1["revision"], "enabled": True}])
    rec = Reconciler(rows, lambda verb, cam: (calls.append(verb), True)[1])
    rec.reconcile(0.0)
    assert calls == ["start"]

    other = base64.b64encode(b"\xff" + bytes(1920 * 1080 // 8 - 1))
    r2 = ctl.update("7-linecross", {"mask": ctl.put_blob(other)})
    assert r2["mask"] != r1["mask"] and r2["revision"] == r1["revision"] + 1

    rows.rows = [{"id": 1, "revision": r2["revision"], "enabled": True}]
    rec.reconcile(1.0)
    assert calls == ["start", "restart"]                         # the new mask, by the mechanism that was there


def test_a_blob_may_not_be_in_the_snapshot():
    """Refused at LOAD time, like a secret — and for a plainer reason: the snapshot
    is one object per worker under a ceiling, and a blob is by definition what did
    not fit in a row."""
    base = {"name": "panel", "unit": {"rows": "panels", "fields": {"host": {"type": "string"},
                                                                   "firmware": {"type": "blob"}}}}
    spec = SubsystemSpec.from_dict(base)
    assert spec.snapshot == ["host"]                              # the default leaves it out rather than refusing
    try:
        SubsystemSpec.from_dict({**base, "snapshot": ["host", "firmware"]})
        raise AssertionError("a blob was accepted into the snapshot")
    except ValueError as e:
        assert "a blob may not be in the snapshot" in str(e)
    assert "mask" not in DET_SPEC.snapshot


def test_the_console_takes_the_bytes_on_their_own_route():
    box = Box()
    ctl, con = _det(box)
    st, row = con.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    assert st == 201
    st, body = con.put_blob("7-linecross", "mask", MASK)
    assert st == 200 and body["mask"] == digest(MASK) and body["bytes"] == len(MASK)
    assert ctl.unit("7-linecross")["mask"] == digest(MASK)
    # a field that is not a blob, and a unit that is not there, are both 404 and not a stack trace
    assert con.put_blob("7-linecross", "params", b"x")[0] == 404
    assert con.put_blob("9-linecross", "mask", b"x")[0] == 404


def test_a_blob_over_the_stores_ceiling_names_the_store():
    """The one place where "change the object store" is the right answer, and the
    message says so: a blob is the class of data an object store exists for, unlike
    the snapshot, which was a shape problem."""
    box = Box()
    ctl, con = _det(box, objects=FsObjectStore(box.root + "/capped", max_bytes=65536 - len("data")))
    con.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    st, body = con.put_blob("7-linecross", "mask", MASK)
    assert st == 413
    assert "OBJECTS=s3+https://" in body["detail"] and "variables:// does not" in body["detail"]
    assert ctl.unit("7-linecross")["mask"] == ""                 # nothing was written, and the row is untouched


def test_what_a_sweep_would_keep_and_the_sweep_that_does_not_exist():
    """`blobs_referenced` is the honest half. Nothing in the platform deletes an
    object, so a replaced mask stays in the store forever and this is the list that
    would tell a collector which ones to keep. Written down rather than implied."""
    box = Box()
    ctl, _ = _det(box)
    ctl.create({"name": "7-linecross", "cam": "7", "kind": "linecross"})
    old = ctl.put_blob(MASK)
    ctl.update("7-linecross", {"mask": old})
    new = ctl.put_blob(base64.b64encode(b"\x01" + bytes(99)))
    ctl.update("7-linecross", {"mask": new})
    stored = {k.rsplit("/", 1)[1] for k in box.objects.list("det/blobs/")}
    assert stored == {old, new}                                  # both still there
    assert ctl.blobs_referenced() == {new}                       # one of them referenced
    assert stored - ctl.blobs_referenced() == {old}              # and this is the garbage nobody collects
