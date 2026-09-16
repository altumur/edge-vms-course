"""What a runtime hands a process — and nothing about WHICH runtime.

    <ROLE>_NAME   the slot to claim outright: systemd's %i, a StatefulSet ordinal,
                  an operator starting one by hand
    SLOT_INDEX    else the index, which the process turns into `<prefix>-<n>`
    SERVER_NAME   whose resource this process writes into, and the host in the URLs
                  it publishes
    LABELS        what this server can reach, comma-separated
    INSTANCE_ID   this incarnation — what failover is measured from

Not one of these names an orchestrator, and that is the whole point of the
module. Quadlet sets `WORKER_NAME=%i`; a Nomad jobspec maps `NOMAD_ALLOC_INDEX`,
`node.unique.name` and `meta.labels` into these; a Kubernetes manifest maps the
ordinal and a `fieldRef`. The loop reads five names and never learns which of
them filled them in — the same rule the package already keeps for the stores
(see `variables.open_vars`).

The index is a PREFERENCE, never proof: whatever a runtime says, the slot is
still taken by CAS (Lesson 7), and a runtime that hands the same index twice
loses the second claim rather than corrupting the first.
"""
from __future__ import annotations

import socket

SLOT_INDEX, SERVER_NAME, LABELS, INSTANCE_ID = "SLOT_INDEX", "SERVER_NAME", "LABELS", "INSTANCE_ID"


# The slot to prefer: an explicit name, else `<prefix>-<index>`, else None — "whichever is free,
# a lapsed one first", so a replacement inherits the assignment.
def slot(env: dict, name_env: str = "WORKER_NAME", prefix: str = "w") -> str | None:
    if env.get(name_env):
        return env[name_env]
    if str(env.get(SLOT_INDEX, "")) != "":
        return f"{prefix}-{int(env[SLOT_INDEX])}"
    return None


# The server this process runs on: what a runtime says, else this host's name. On one box the
# hostname is right and no runtime has to say anything.
def server(env: dict, given: str | None = None) -> str:
    return given or env.get(SERVER_NAME) or socket.gethostname()


def labels(env: dict, default: str = "") -> list[str]:
    return [l for l in env.get(LABELS, default).split(",") if l]


# This incarnation. `None` lets the worker fall back to the base class's `hostname:pid:6hex`,
# which is enough to tell one instance from the next on a box.
def instance(env: dict) -> str | None:
    return env.get(INSTANCE_ID) or None
