"""Lesson 9 — an edit for a cluster that is off.

Lesson 3 forwarded an edit to the owning cluster and, when that cluster did not answer, said 503: *could not
look*. That is right for a server room, which is rarely wholly unreachable, and wrong for a camera, which is
one device and is off often — power, a PoE switch, maintenance, a remote site. And cameras are edited in bulk.

So the domain KEEPS the edit: per field, the value it last saw and the value wanted, in the domain cluster's
Variables beside the grants. The cluster's own agent carries it home when the cluster comes back, and the
cluster's own console applies it — as the operator, with that operator's grant checked then, and only where
the field still holds the value the edit was based on. The owner never changes; one writer per key stands.
"""
import json

from domain.agent import DomainAgent
from domain.api import ApiError, ConsoleAPI
from domain.federation import DomainDirectory
from domain.pending import PendingEdits
from domain.readview import ReadView
from tests.conftest import Clock, make_domain

CAM = "4471"                                                     # the domain's name for the camera — its ref


def _snapshot(cluster, rows: dict[str, dict], ts: float) -> None:
    """A member's snapshot carrying the fields an operator edits, the way a controller publishes it."""
    cameras = [{"id": i + 1, "ref": ref, "worker": "w-0", "server": "srv-1", **fields}
               for i, (ref, fields) in enumerate(rows.items())]
    cluster.objects.put("vms/snapshot/w-0", json.dumps({"cluster": cluster.name, "worker": "w-0", "ts": ts,
                                                        "cameras": cameras}).encode())


class Member:
    """A member cluster as its agent reaches it: the rows it holds, by the domain's ref, and its console —
    the only writer of those rows — which checks the subject's grant the way a cluster console does."""

    def __init__(self, rows: dict[str, dict], allowed: set | None = None):
        self.rows, self.allowed, self.edits = rows, allowed, []

    def current(self, ref):
        row = self.rows.get(str(ref))
        return (row["id"], row) if row else None

    def update_camera(self, camera, fields, subject):
        if self.allowed is not None and subject not in self.allowed:
            raise ApiError(403, f"{subject} has no grant on camera {camera}")
        row = next(r for r in self.rows.values() if r["id"] == camera)
        row.update(fields)
        self.edits.append((camera, dict(fields), subject))
        return {"revision": len(self.edits) + 1}

    def create_camera(self, fields, subject):
        raise AssertionError("not in this lesson")


def _domain_with_a_camera_that_went_off(fields=None):
    """The domain saw camera 4471 while it was on — its row is in the read view — and then it was switched
    off. The domain cluster is `north`; the camera is a cluster of its own."""
    wall = Clock(10_000.0)
    fed, links = make_domain({"north": (), "cam-4471": ()}, "north")
    _snapshot(fed.clusters["cam-4471"], {CAM: fields or {"name": "gate", "events_retention_days": 30}}, wall())
    view = ReadView(fed, wall=wall)
    view.refresh()
    links["cam-4471"].up = False
    view.refresh()
    pending = PendingEdits(fed.domain_cluster.vars, wall)
    api = ConsoleAPI(DomainDirectory(fed), lambda name: (_ for _ in ()).throw(AssertionError("owner is off")),
                     verifier=lambda token: token, pending=pending, last_known=view.last_known)
    return wall, fed, links, pending, api


def _agent(fed, member, wall):
    return DomainAgent("cam-4471", fed.domain_cluster.vars, fed.clusters["cam-4471"].vars, now=wall,
                       console=member, current=member.current)


def test_an_edit_for_a_camera_that_is_off_is_kept_not_refused():
    """Lesson 3 said 503 here, and it was honest: the owner could not be asked. For a camera that is off it
    is also useless — the operator must remember to come back. The domain keeps the edit instead, and says
    it is waiting: accepted is not applied, and the response must not look like the one that was."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    r = api.update_camera(CAM, {"events_retention_days": 7}, idempotency_key="k1", token="anna")
    assert r["pending"] is True and r["cluster"] == "cam-4471"
    entry = pending.of("cam-4471")[CAM]
    assert entry["fields"]["events_retention_days"] == {"old": 30, "new": 7}   # the value it was based on, and the wanted one
    assert entry["subject"] == "anna"


def test_the_camera_that_comes_back_takes_the_edit_from_its_own_agent():
    """When the camera is back its agent — the domain's one right inside it, `domain/*` — carries the edit
    home, and the camera's own console applies it. As the operator who made it, not as the agent: the row
    has one writer, and the edit is that operator's. Then the domain reads the outcome and clears what
    landed."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    api.update_camera(CAM, {"events_retention_days": 7}, idempotency_key="k1", token="anna")

    member = Member({CAM: {"id": 1, "ref": CAM, "name": "gate", "events_retention_days": 30}})
    links["cam-4471"].up = True
    assert _agent(fed, member, wall).sync()
    assert member.rows[CAM]["events_retention_days"] == 7
    assert member.edits == [(1, {"events_retention_days": 7}, "anna")]         # applied AS anna

    pending.collect(fed)
    assert pending.of("cam-4471") == {}


def test_a_field_changed_on_the_camera_meanwhile_is_a_conflict_not_an_overwrite():
    """While it was off, somebody on site renamed it — on the camera's own page, which is still its console.
    The domain's edit was based on the old name. Overwriting it would throw away a change nobody at the
    domain saw; refusing the whole edit would throw away the retention change for the sake of a field it
    never touched. So the comparison is per field: a field that still holds the value the edit was based
    on takes the new one, and a field that moved underneath it is a conflict, shown, not decided."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    api.update_camera(CAM, {"name": "main-gate", "events_retention_days": 7}, idempotency_key="k1", token="anna")

    member = Member({CAM: {"id": 1, "ref": CAM, "name": "gate-B", "events_retention_days": 30}})   # renamed on site
    links["cam-4471"].up = True
    _agent(fed, member, wall).sync()
    assert member.rows[CAM] == {"id": 1, "ref": CAM, "name": "gate-B", "events_retention_days": 7}

    pending.collect(fed)
    left = pending.of("cam-4471")[CAM]["fields"]
    assert list(left) == ["name"] and left["name"]["conflict"] == "gate-B"     # retention landed; the name waits for a person


def test_a_person_resolving_a_conflict_edits_against_what_the_camera_now_holds():
    """"Apply again" means apply against what is THERE now. A second edit to a conflicted field takes the
    camera's current value as the one it is based on — otherwise it would conflict again for ever."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    api.update_camera(CAM, {"name": "main-gate"}, idempotency_key="k1", token="anna")
    member = Member({CAM: {"id": 1, "ref": CAM, "name": "gate-B", "events_retention_days": 30}})
    links["cam-4471"].up = True
    _agent(fed, member, wall).sync()
    pending.collect(fed)

    links["cam-4471"].up = False
    api.update_camera(CAM, {"name": "main-gate"}, idempotency_key="k2", token="anna")   # "apply again"
    assert pending.of("cam-4471")[CAM]["fields"]["name"] == {"old": "gate-B", "new": "main-gate"}
    links["cam-4471"].up = True
    _agent(fed, member, wall).sync()
    assert member.rows[CAM]["name"] == "main-gate"


def test_edits_made_while_it_is_off_merge_per_field():
    """Two edits before the camera comes back are not a queue to replay: the camera needs the last value of
    each field. Measured against what the domain last saw — which, while the camera is off, cannot move,
    since nothing new arrives from it — and remembering the value in between, which may already have landed
    (see the next test but one)."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    api.update_camera(CAM, {"events_retention_days": 7}, idempotency_key="k1", token="anna")
    api.update_camera(CAM, {"events_retention_days": 14, "name": "main-gate"}, idempotency_key="k2", token="anna")
    fields = pending.of("cam-4471")[CAM]["fields"]
    assert fields["events_retention_days"] == {"old": 30, "new": 14, "via": [7]}   # the first edit's base, the last edit's value,
                                                                                   # and the value in between — it may already have landed
    assert fields["name"] == {"old": "gate", "new": "main-gate"}


def test_the_grant_is_checked_when_it_is_applied_not_when_it_was_accepted():
    """An edit can wait a week, and a week is long enough for an operator to lose the right to make it. The
    cluster's console checks the grant at the moment it applies, against its own grants — the same check
    as a live edit — and a refused edit is kept and said, not dropped and not forced."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    api.update_camera(CAM, {"events_retention_days": 7}, idempotency_key="k1", token="anna")
    member = Member({CAM: {"id": 1, "ref": CAM, "name": "gate", "events_retention_days": 30}}, allowed={"boris"})
    links["cam-4471"].up = True
    _agent(fed, member, wall).sync()
    assert member.rows[CAM]["events_retention_days"] == 30 and member.edits == []

    pending.collect(fed)
    entry = pending.of("cam-4471")[CAM]
    assert "no grant" in entry["refused"]


def test_carrying_it_home_twice_applies_it_once():
    """The agent syncs on a timer and the domain collects on its own, so an edit can reach the camera twice
    before the domain has cleared it. The per-field comparison makes that harmless: the second time the
    field already holds the wanted value, and nothing is written."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    api.update_camera(CAM, {"events_retention_days": 7}, idempotency_key="k1", token="anna")
    member = Member({CAM: {"id": 1, "ref": CAM, "name": "gate", "events_retention_days": 30}})
    links["cam-4471"].up = True
    agent = _agent(fed, member, wall)
    agent.sync(); agent.sync()
    assert len(member.edits) == 1


def test_without_a_place_to_keep_edits_the_api_still_says_503():
    """Lesson 3's answer is not wrong, it is the answer of an API with nowhere to put the edit. Without a
    pending store the domain says what it always said — could not look — rather than pretend to keep it."""
    wall, fed, links, pending, _ = _domain_with_a_camera_that_went_off()
    api = ConsoleAPI(DomainDirectory(fed), lambda name: None)
    try:
        api.update_camera(CAM, {"events_retention_days": 7}, idempotency_key="k1")
        raise AssertionError("must refuse")
    except ApiError as e:
        assert e.status == 503


def test_an_edit_made_before_the_last_one_was_confirmed_is_not_a_conflict():
    """The agent applies an edit and reports it; the domain reads the report later. In between the camera can
    go off again and the operator edit the same field again. The merged edit is still based on the value from
    before the FIRST edit — but the camera took the first edit, and now holds a value the domain itself sent.
    That is not a conflict. So each field remembers the values it has already sent, and a camera holding one
    of them holds a base, not a surprise.

    And the report about the first edit, read after the second was made, must clear nothing: it names the
    edit it was about (`rev`), and it was not about this one."""
    wall, fed, links, pending, api = _domain_with_a_camera_that_went_off()
    api.update_camera(CAM, {"events_retention_days": 7}, idempotency_key="k1", token="anna")
    member = Member({CAM: {"id": 1, "ref": CAM, "name": "gate", "events_retention_days": 30}})
    agent = _agent(fed, member, wall)
    links["cam-4471"].up = True
    agent.sync()                                                   # the camera took 7, and said so in its own Variables
    links["cam-4471"].up = False                                   # …and went off before the domain read it

    api.update_camera(CAM, {"events_retention_days": 14}, idempotency_key="k2", token="anna")
    links["cam-4471"].up = True
    pending.collect(fed)                                           # the report is about the FIRST edit
    assert "events_retention_days" in pending.of("cam-4471")[CAM]["fields"], "a report about an older edit cleared a newer one"

    agent.sync()
    assert member.rows[CAM]["events_retention_days"] == 14, "the camera's own earlier value was read as somebody else's change"
    pending.collect(fed)
    assert pending.of("cam-4471") == {}
