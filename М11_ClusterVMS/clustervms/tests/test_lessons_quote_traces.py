"""A request quoted in a lesson is a request the stand made.

The lessons quote the stand's traces: request lines (`PUT /v1/var/...?namespace=...`) and answers that carry a
`ModifyIndex`. `test_stand.py` keeps the traces equal to the code; this keeps the lessons equal to the
traces. A quoted line that is in no trace file — an index that shifted, a key that was renamed — fails here,
with the lesson and the line. Lines shortened with `...` are paraphrase and are not checked.
"""
import glob
import os
import re

from tests.stand import TRACES

MODULE = os.path.dirname(TRACES)
REQUEST = re.compile(r"^(GET|PUT|DELETE) /v1/.*namespace=")
ANSWER = re.compile(r'^→ \d{3} \{"Path".*"ModifyIndex": \d+\}$')


def test_every_quoted_request_and_index_is_in_a_trace():
    traces = "\n".join(open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(TRACES, "*.txt")))
    lines = set(traces.splitlines())
    missing = []
    for lesson in sorted(glob.glob(os.path.join(MODULE, "[0-9][0-9]-*.md"))):
        for n, line in enumerate(open(lesson, encoding="utf-8").read().splitlines(), 1):
            line = line.strip()
            if "..." in line or "…" in line:
                continue
            if (REQUEST.match(line) or ANSWER.match(line)) and line not in lines:
                missing.append(f"{os.path.basename(lesson)}:{n}: {line}")
    assert not missing, "quoted in a lesson, made by no scene:\n" + "\n".join(missing)
