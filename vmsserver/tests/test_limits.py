"""The ceiling a store declares, and what the platform does when a write is over it.

Before this, the 64 KiB of a Nomad Variable lived in prose. `FsObjectStore`
accepted anything, so a write that production would reject was green here, and
the platform found its ceiling the way you find a ceiling in the dark — the
snapshot (Lesson 25) was found by arithmetic on paper, not by a failing test.

Now the store says what it can hold and refuses more. These tests build stores
that DO have a ceiling, which is the only way the refusal is observable on one
box.
"""
import json

from vms.config import SPEC
from vms.controller import VmsController
from w2cplatform.console import SpecConsole
from w2cplatform.objects import FsObjectStore
from w2cplatform.variables import FileVariables, items_bytes
from tests.conftest import Box


def test_a_store_with_no_ceiling_still_answers_the_question():
    """`max_bytes` is an ANSWER, not an attribute that may be missing. A directory
    has no ceiling and says 0; the platform can ask any store and get a number."""
    box = Box()
    assert box.objects.max_bytes == 0 and box.vars.max_bytes == 0


def test_the_object_store_refuses_over_its_ceiling_and_keeps_what_was_there():
    box = Box()
    small = FsObjectStore(box.root + "/capped", max_bytes=1024)
    small.put("k", b"x" * 1000)
    try:
        small.put("k", b"x" * 2000)
        raise AssertionError("a write over the store's own ceiling was accepted")
    except Exception as e:                                        # noqa: BLE001
        assert type(e).__name__ == "TooLarge" and e.size == 2000 and e.limit == 1024
    assert small.get("k") == b"x" * 1000                          # refused, not truncated


def test_an_oversized_shard_says_how_many_units_that_was():
    """The store refuses with bytes. The caller knows what those bytes were, and a
    shard is ONE worker's assignment — so an oversized shard is no longer a shape
    problem to be solved by cutting differently. It is a store too small to hold
    what a single worker carries, and the message says which knob that is."""
    box = Box()
    box.objects = FsObjectStore(box.root + "/tiny", max_bytes=512)
    ctl = VmsController(box.vars, box.objects, capacity=50, wall=box.wall)
    for i in range(20):
        ctl.create_camera({"source": f"driverpack://file/{i}.mp4"})
    ctl.ensure_placed(workers=["w-1"])
    try:
        ctl.publish_snapshot()
        raise AssertionError("a shard over the store's ceiling was published")
    except Exception as e:                                        # noqa: BLE001
        assert type(e).__name__ == "TooLarge"
        assert "20 units on w-1" in str(e)
        assert "OBJECTS=" in str(e)                               # the knob, named where a person reads it


def test_a_row_that_does_not_fit_is_a_413_to_the_person_who_typed_it():
    """The worst version of this defect is the silent one. A row over the store's
    ceiling is refused at the console, with the size and the limit in the sentence,
    before anything is written — not discovered later by whoever reads the row."""
    box = Box()
    capped = FileVariables(box.root + "/capped-vars", max_bytes=400)
    ctl = VmsController(capped, box.objects, wall=box.wall)
    con = SpecConsole(ctl)
    st, body = con.create({"name": "gate", "source": "driverpack://file/gate.mp4"})
    assert st == 201
    big = {"labels": ["x" * 500]}
    st, body = con.update(body["id"], big)
    assert st == 413, (st, body)
    assert "over this store's limit of 400" in body["detail"]
    # and the row is exactly what it was
    assert ctl.camera(1)["labels"] == []


def test_the_size_a_store_charges_is_keys_and_values():
    """How Nomad measures a Variable, and therefore how the platform must: every
    key and every value in the path, together — not the JSON around them."""
    assert items_bytes({"a": "xx", "bb": "y"}) == 1 + 2 + 2 + 1
    row = SPEC.items({"id": 1, "name": "gate", "source": "driverpack://file/g.mp4", "revision": 1})
    assert 100 < items_bytes(row) < 400, items_bytes(row)         # a row is small, and that is the point
