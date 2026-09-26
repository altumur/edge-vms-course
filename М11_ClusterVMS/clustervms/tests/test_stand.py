"""The traces the lessons quote are records of runs, and they stay that way.

`tests/stand.py` runs each scene of the module's story on the traced stand; the full traces are stored in
`М11_ClusterVMS/traces/`. This test runs every scene again and compares. When the code changes what it asks
of the store, this fails — and the lesson that quotes the trace is the next thing to update, with
`python3 tests/stand.py --write`.
"""
import os

from tests.stand import SCENES, TRACES


def test_every_stored_trace_is_what_the_code_does_now():
    stale = []
    for name, scene in SCENES.items():
        path = os.path.join(TRACES, name + ".txt")
        with open(path, encoding="utf-8") as f:
            if f.read() != scene():
                stale.append(name)
    assert not stale, f"traces no longer match the code: {stale} — regenerate with tests/stand.py --write and update the lessons that quote them"
