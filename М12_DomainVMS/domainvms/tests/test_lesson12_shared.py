"""Lesson 12 — shared settings without a database.

Defaults, the folder tree, the scenarios between cameras: settings of the system, not of a camera. The
domain publishes them the way it publishes the identity set — one object, named by a pointer, object
first — signed, so that a copy anywhere is as good as the original, and carried by every member's agent
into that member's own durable store, where the member reads them with the domain gone.
"""
import json

from cluster.variables import Conflict, FakeVariables
from w2cplatform.variables import items_bytes

from domain.agent import DomainAgent, DomainPublisher
from domain.device import DeviceCluster
from domain.federation import Federation
from domain.shared import OBJECT, POINTER, SharedSettings, SharedView, sign
from domain.signer import Signer
from domain.tokens import TokenIssuer
from tests.conftest import Clock, make_cluster


def _site(wall, n=3):
    fed = Federation()
    north, north_link = make_cluster("north", domain=True)
    fed.add(north)
    signer = Signer("acme", north.vars, now=wall)
    DomainPublisher(north.vars).publish_keys(signer.tokens.keyset())
    devices, agents = [], []
    for i in range(n):
        d = DeviceCluster(f"SN{i}", FakeVariables(), wall=wall)
        d.boot()
        fed.add(d.cluster())
        devices.append(d)
        agents.append(DomainAgent(d.name, north.vars, d.flash, now=wall, domain_objects=north.objects,
                                  cluster_objects=d.disk))
    shared = SharedSettings(north.vars, north.objects, signer.tokens, wall=wall)
    return fed, north, north_link, signer, shared, devices, agents


def _retention(days):
    return lambda s: s.setdefault("defaults", {}).update(events_retention_days=days)


def test_published_once_carried_by_every_agent_and_the_console_says_where():
    """One edit, one object, one pointer. Every member's agent takes it on its next pass. The console
    shows delivery the way Lesson 9 shows kept edits — from each member's own report — and names the
    member that is not answering instead of rounding it into the total."""
    wall = Clock()
    fed, north, _, signer, shared, devices, agents = _site(wall)
    rev = shared.edit(_retention(14), base_rev=0, by="anna")
    devices[2].power_off()
    for a in agents[:2]:
        a.sync()
    d = shared.delivery(fed)
    assert d["holding"] == ["cam-SN0", "cam-SN1"] and d["silent"] == ["cam-SN2"]
    assert d["sentence"] == "rev 1 on 2 of 3 members; not answering: cam-SN2"

    devices[2].boot()
    agents[2].sync()
    assert shared.delivery(fed)["holding"] == ["cam-SN0", "cam-SN1", "cam-SN2"]
    assert SharedView(devices[2].flash, devices[2].disk, wall).settings()["defaults"]["events_retention_days"] == 14 and rev == 1


def test_a_document_not_signed_by_the_domain_is_refused_and_the_old_one_kept():
    """Whoever can write the domain's store is not thereby the domain. A document signed by another key,
    with a pointer whose checksum matches it perfectly, is refused by every member — against the key set
    ITS agent carried, never one that came with the document — and the member keeps what it had. The
    refusal is reported, with its reason, where the domain reads it."""
    wall = Clock()
    fed, north, _, signer, shared, devices, agents = _site(wall)
    shared.edit(_retention(14), base_rev=0)
    for a in agents:
        a.sync()

    forged = sign({"rev": 2, "term": 1, "settings": {"defaults": {"events_retention_days": 1}}}, TokenIssuer("acme"))
    raw = json.dumps(forged, sort_keys=True).encode()
    north.objects.put("shared/rev-2", raw)
    import hashlib
    _, idx = north.vars.get(POINTER)
    north.vars.put(POINTER, {"object": "shared/rev-2", "rev": 2, "term": 1, "sha256": hashlib.sha256(raw).hexdigest()}, cas=idx)
    agents[0].sync()
    assert agents[0].shared.startswith("refused") and "does not trust" in agents[0].shared
    assert SharedView(devices[0].flash, devices[0].disk, wall).settings()["defaults"]["events_retention_days"] == 14
    assert "cam-SN0" in shared.delivery(fed)["refused"]


def test_an_older_document_never_replaces_a_newer_one():
    """The pointer can go backwards — a domain restored from yesterday's backup points at yesterday's
    revision. A member that already holds a newer one keeps it: (term, rev) only ever grows on a member,
    whatever the domain says today. Lesson 15 is what makes the domain's own number grow again."""
    wall = Clock()
    fed, north, _, signer, shared, devices, agents = _site(wall, n=1)
    shared.edit(_retention(14), base_rev=0)
    ptr_rev1, _ = north.vars.get(POINTER)
    shared.edit(_retention(30), base_rev=1)
    agents[0].sync()
    _, idx = north.vars.get(POINTER)
    north.vars.put(POINTER, ptr_rev1, cas=idx)           # the domain, restored from before rev 2
    agents[0].sync()
    assert agents[0].shared == "holding newer"
    assert SharedView(devices[0].flash, devices[0].disk, wall).settings()["defaults"]["events_retention_days"] == 30


def test_a_default_is_resolved_when_read_and_never_written_into_a_row():
    """Changing the default retention must not be five hundred writes to five hundred rows — some of them
    on cameras that are off, all of them by a writer that is not the row's. The row keeps what the camera
    set; the console resolves the rest against the document and says where each value came from."""
    wall = Clock()
    fed, north, _, signer, shared, devices, agents = _site(wall, n=2)
    devices[1].update_camera(1, {"events_retention_days": 30}, None)          # set on the camera's own page
    before = [d.row()["revision"] for d in devices]
    shared.edit(_retention(14), base_rev=0)
    for a in agents:
        a.sync()
    assert [d.row()["revision"] for d in devices] == before                    # no row was touched
    eff = [SharedView(d.flash, d.disk, wall).effective(d.row())["events_retention_days"] for d in devices]
    assert eff == [(14, "domain rev 1"), (30, "camera")]


def test_two_editors_of_the_shared_settings_are_told_not_overwritten():
    wall = Clock()
    fed, north, _, signer, shared, devices, agents = _site(wall, n=1)
    shared.edit(_retention(14), base_rev=0, by="anna")
    try:
        shared.edit(_retention(7), base_rev=0, by="boris")                     # boris was looking at rev 0
        raise AssertionError("boris must be told")
    except Conflict:
        pass
    assert shared.current()[0]["settings"]["defaults"]["events_retention_days"] == 14


def test_a_five_hundred_camera_tree_does_not_fit_a_variable_and_does_not_need_to():
    """Five hundred folders and five hundred scenarios are some eighty kilobytes: over the 64 KiB a Nomad
    Variable may hold, whole. The document is an object, so its size is the object store's business; the
    Variable is the pointer, and the pointer is a hundred bytes whatever the tree grows to."""
    wall = Clock()
    fed, north, _, signer, shared, devices, agents = _site(wall, n=1)

    def big(s):
        s["folders"] = [f"Site-{i // 100:02d}/Building-{i // 20 % 5}/Floor-{i // 5 % 4}/Zone-{i:03d}" for i in range(500)]
        s["scenarios"] = [{"when": {"camera": f"SN{i}", "kind": "motion"}, "then": {"camera": f"SN{i + 1}", "action": "preset", "arg": 3}}
                          for i in range(500)]
    shared.edit(big, base_rev=0)
    assert items_bytes(shared.current()[0]["settings"]) > 65536
    assert items_bytes(north.vars.get(POINTER)[0]) < 200
    agents[0].sync()
    assert len(SharedView(devices[0].flash, devices[0].disk, wall).settings()["folders"]) == 500


def test_with_the_domain_gone_a_camera_that_reboots_still_has_its_defaults():
    """The thesis, for settings: the domain can be switched off and nothing below notices. The document is
    in the camera's durable store, checked again on the way in, and a reboot changes nothing."""
    wall = Clock()
    fed, north, north_link, signer, shared, devices, agents = _site(wall, n=1)
    shared.edit(_retention(14), base_rev=0)
    agents[0].sync()
    north_link.up = False
    assert agents[0].sync() is False
    devices[0].power_off(); devices[0].boot()
    assert SharedView(devices[0].flash, devices[0].disk, wall).effective(devices[0].row())["events_retention_days"] == (14, "domain rev 1")
    assert devices[0].disk.get(OBJECT) is not None
