"""The policies and the code say the same thing, and a test says so.

Variables have had an ACL since М10A Lesson 1, and it is derived from the spec:
`acl_controller()`, `acl_console()`, `acl_worker()`. The OBJECT STORE never had
one. On one box that is invisible — `FsObjectStore` has no writer and no prefixes
— and on a cluster the ACL is real, enforced by the scheduler, but written by
hand in `deploy/*-policy.hcl`.

Nothing checked that the hand-written file still matched the code, and inside two
commits it stopped matching three times:

  * the controller's grant named `objects/vms/snapshot` exactly, and the snapshot
    became one object per worker — every shard a 403, every five seconds, logged
    as "placement pass failed";
  * the console got a `blob` route and no grant to write `objects/vms/blobs/*`,
    so the route could not work on a cluster at all;
  * the rec controller never had a grant for its own snapshot, from the day it
    was written.

None of the three is subtle. All three were invisible because the suite runs
against a store with no ACL, and the file with the ACL is not code.

So: the grants are derived from the spec (`acl_objects_*`), and this checks each
policy against them in BOTH directions — everything the code writes is allowed,
and nothing else is.
"""
import os
import re

from vms.config import REC_SPEC, SPEC
from w2cplatform.blobs import digest

DEPLOY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy")
OBJECTS = "objects/"                       # VariablesObjectStore's prefix: an object IS a Variable here

_RULE = re.compile(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]', re.S)


def rules(policy: str) -> list[tuple[str, set[str]]]:
    """`[(path pattern, capabilities)]` in file order, which is the order Nomad reads them."""
    src = open(os.path.join(DEPLOY, policy), encoding="utf-8").read()
    return [(p, {c.strip().strip('"') for c in caps.split(",") if c.strip()}) for p, caps in _RULE.findall(src)]


def matches(pattern: str, key: str) -> bool:
    """Nomad's glob: `*` stands for any run of characters, and a pattern with no
    `*` is an EXACT path. That second half is the whole of the first bug — a
    pattern naming `objects/vms/snapshot` does not match `objects/vms/snapshot/w-1`."""
    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), key) is not None


def may_write(policy: str, key: str) -> bool:
    return any("write" in caps for pat, caps in rules(policy) if matches(pat, key))


def write_patterns(policy: str) -> set[str]:
    return {pat for pat, caps in rules(policy) if "write" in caps}


def _keys(prefixes: list[str], sample: str) -> list[str]:
    """A concrete key under each granted prefix — a pattern is checked by what it
    has to match, not by comparing two strings that look alike."""
    return [OBJECTS + p.rstrip("*") + sample for p in prefixes]


def test_every_object_the_code_writes_is_granted():
    """Direction one: take the keys the platform actually writes and find them a rule."""
    cases = [
        ("vmsworker-policy.hcl", _keys(SPEC.sub.acl_objects_worker(), "w-1")),
        ("recworker-policy.hcl", _keys(REC_SPEC.sub.acl_objects_worker(), "r-1")),
        ("vmscontroller-policy.hcl", _keys(SPEC.sub.acl_objects_controller(), "w-1")
                                     + _keys(SPEC.sub.acl_objects_controller(), "unplaced")),
        ("reccontroller-policy.hcl", _keys(REC_SPEC.sub.acl_objects_controller(), "r-1")),
        ("console-policy.hcl", _keys(SPEC.sub.acl_objects_console(), digest(b"a mask"))
                               + _keys(REC_SPEC.sub.acl_objects_console(), digest(b"a mask"))),
    ]
    for policy, keys in cases:
        for key in keys:
            assert may_write(policy, key), f"{policy} does not let it write {key}"


def test_nothing_writes_an_object_it_has_no_business_writing():
    """Direction two, and the one that costs something to keep true: a grant wider
    than the code needs is how a worker ends up able to write М12's directory."""
    denied = [
        # a worker writes its own heartbeat and nothing else in the subsystem
        ("vmsworker-policy.hcl", OBJECTS + "vms/snapshot/w-1"),
        ("vmsworker-policy.hcl", OBJECTS + "vms/blobs/" + digest(b"a mask")),
        ("recworker-policy.hcl", OBJECTS + "rec/snapshot/r-1"),
        # the controller publishes the snapshot and touches neither of the others
        ("vmscontroller-policy.hcl", OBJECTS + "vms/heartbeats/w-1"),
        ("vmscontroller-policy.hcl", OBJECTS + "vms/blobs/" + digest(b"a mask")),
        # the console owns the rows and the blobs, never a heartbeat or a shard
        ("console-policy.hcl", OBJECTS + "vms/heartbeats/w-1"),
        ("console-policy.hcl", OBJECTS + "vms/snapshot/w-1"),
        # and the resource stays inside its own prefix
        ("resource-policy.hcl", OBJECTS + "vms/heartbeats/w-1"),
        ("resource-policy.hcl", OBJECTS + "vms/snapshot/w-1"),
    ]
    for policy, key in denied:
        assert not may_write(policy, key), f"{policy} lets it write {key}"


def test_every_write_grant_is_one_the_code_asked_for():
    """The strongest half: no rule in the file that nothing in the code explains.
    This is what `objects/vms/*` would fail — the grant the worker had until now,
    whose comment said "its heartbeat" while the pattern said the whole subsystem."""
    expected = {
        "vmsworker-policy.hcl": set(SPEC.sub.acl_worker()) | {OBJECTS + p for p in SPEC.sub.acl_objects_worker()},
        "recworker-policy.hcl": set(REC_SPEC.sub.acl_worker()) | {OBJECTS + p for p in REC_SPEC.sub.acl_objects_worker()},
        "vmscontroller-policy.hcl": set(SPEC.acl_controller()) | {OBJECTS + p for p in SPEC.sub.acl_objects_controller()},
        "reccontroller-policy.hcl": set(REC_SPEC.acl_controller()) | {OBJECTS + p for p in REC_SPEC.sub.acl_objects_controller()},
        "console-policy.hcl": set(SPEC.acl_console()) | set(REC_SPEC.acl_console())
                              | {OBJECTS + p for p in SPEC.sub.acl_objects_console()}
                              | {OBJECTS + p for p in REC_SPEC.sub.acl_objects_console()},
    }
    for policy, allowed in expected.items():
        unexplained = write_patterns(policy) - allowed
        assert not unexplained, f"{policy} grants write to {sorted(unexplained)}, which no acl_* in the spec asks for"


def test_the_exact_path_that_broke_it_stays_broken_as_a_pattern():
    """Kept as a named case because it cost nothing to write and would have cost a
    production morning: Nomad's path is a glob, and a glob without a `*` is exact."""
    assert matches("objects/vms/snapshot/*", "objects/vms/snapshot/w-1")
    assert not matches("objects/vms/snapshot", "objects/vms/snapshot/w-1")
    assert matches("objects/vms/*", "objects/vms/snapshot/w-1")          # why the old worker grant was too wide
