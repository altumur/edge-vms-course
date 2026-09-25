"""Lesson 15 — a domain cluster of one node.

A site of cameras and no server: the domain's services run on one camera. Re-hosting them becomes an
ordinary operation — from the signer's key, kept beyond the host since Lesson 7, and the newest signed copy
of the domain's state that other members keep — and it needs a term, so that a host which comes back after
being replaced steps down instead of splitting the site in two.
"""
import json

from cluster.variables import FakeVariables

from domain.agent import DomainAgent, DomainPublisher
from domain.api import ApiError, ConsoleAPI
from domain.device import DeviceCluster
from domain.federation import DomainDirectory, Federation
from domain.grants import Grant
from domain.pending import PendingEdits
from domain.readview import ReadView
from domain.shared import sign
from domain.signer import Signer
from domain.term import (BACKUP, Deposed, DomainHost, GuardedPending, carry_host, find_host, handover, read_host,
                         rehost, stranded)
from domain.tokens import TokenIssuer
from tests.conftest import Clock

DOMAIN = "acme"


def _site(wall, n=4):
    fed, devices = Federation(), {}
    for i in range(n):
        d = DeviceCluster(f"SN{i}", FakeVariables(), wall=wall)
        d.boot()
        devices[d.name] = d
        fed.add(d.cluster(domain=i == 0))
    home = fed.clusters["cam-SN0"].vars
    signer = Signer(DOMAIN, home, now=wall)
    offline = signer.backup()                            # Lesson 7: the key, kept beyond the host
    DomainPublisher(home).publish_keys(signer.tokens.keyset())
    for name in devices:
        DomainPublisher(home).publish_grants(name, [Grant("anna", "edit", None, wall() + 30 * 86400)])
    host = DomainHost(fed, "cam-SN0", signer, term=1, wall=wall)
    host.claim()
    agents = {name: _agent(fed, devices, name, "cam-SN0", wall) for name in devices if name != "cam-SN0"}
    for a in agents.values():
        a.sync()
    return fed, devices, signer, offline, host, agents


def _agent(fed, devices, name, host, wall):
    d = devices[name]
    return DomainAgent(name, fed.clusters[host].vars, d.flash, now=wall, console=d, current=d.current,
                       domain_objects=devices[host].disk_door(), cluster_objects=d.disk)


def _keep_an_edit_for(fed, devices, camera, host_vars, wall):
    view = ReadView(fed, wall=wall)
    view.refresh()
    devices[f"cam-{camera}"].power_off()
    view.refresh()
    api = ConsoleAPI(DomainDirectory(fed, wall=wall), lambda n: devices[n], verifier=lambda t: t,
                     pending=PendingEdits(host_vars, wall), last_known=view.last_known)
    return api.update_camera(camera, {"name": f"{camera}-renamed"}, idempotency_key=f"k-{camera}", token="anna")


def _objects(devices):
    return lambda name: devices[name].disk_door()


def test_the_host_dies_and_the_domain_is_rehosted_with_the_edit_it_was_keeping():
    """SN3 is off and the domain on SN0 is keeping an edit for it — the one piece of state that, by
    definition, is on no camera that could carry it home. SN0 published a signed backup to SN1 and SN2.
    Then SN0 dies. The operator re-hosts on SN1 from the signer's key: term 2, the newest backup any member
    holds, and the kept edit with it. SN3 boots, its agent finds the host with the larger term, and the
    edit lands."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)
    assert _keep_an_edit_for(fed, devices, "SN3", host.vars, wall)["pending"]
    assert host.backup(["cam-SN1", "cam-SN2"], devices["cam-SN0"].disk_door()) == 1
    agents["cam-SN1"].sync(); agents["cam-SN2"].sync()
    devices["cam-SN0"].power_off()                       # for good: flash worn out

    new, report = rehost(fed, "cam-SN1", offline, DOMAIN, _objects(devices), wall)
    assert (report["term"], report["rev"]) == (2, 1) and report["restored_from"] in ("cam-SN1", "cam-SN2")
    assert "SN3" in PendingEdits(new.vars, wall).of("cam-SN3")
    assert fed.domain_cluster.name == "cam-SN1"

    d3 = devices["cam-SN3"]
    d3.boot()
    keys = signer.tokens.keyset()
    assert find_host(fed, d3.flash, keys, wall()) == "cam-SN1"
    _agent(fed, devices, "cam-SN3", "cam-SN1", wall).sync()
    assert d3.row()["name"] == "SN3-renamed"


def test_a_host_record_is_never_carried_backwards():
    """The old host comes back and an agent still pointed at it carries its record. Carried blindly, term 1
    would overwrite term 2 on every member that agent reached and undo the re-host. The record is carried
    like the keys, with one more rule: never to a smaller term."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)
    host.backup(["cam-SN1"], devices["cam-SN0"].disk_door()); agents["cam-SN1"].sync()
    devices["cam-SN0"].power_off()
    rehost(fed, "cam-SN1", offline, DOMAIN, _objects(devices), wall)
    d2 = devices["cam-SN2"]
    _agent(fed, devices, "cam-SN2", "cam-SN1", wall).sync()
    keys = signer.tokens.keyset()
    assert read_host(d2.flash, keys, wall())["term"] == 2

    devices["cam-SN0"].boot()                            # the old host is back, still claiming term 1
    assert carry_host(fed.clusters["cam-SN0"].vars, d2.flash, keys, wall()) == "holding a larger term"
    assert read_host(d2.flash, keys, wall())["term"] == 2
    assert find_host(fed, d2.flash, keys, wall()) == "cam-SN1"


def test_an_old_host_that_comes_back_steps_down_and_lists_what_it_alone_holds():
    """SN0 kept an edit for SN2 AFTER its last backup, then died. The new term does not have it and cannot:
    it was on no other camera. When SN0 comes back it reads a larger term on a member and steps down — its
    writes refused, saying where they go — and what it alone held is listed for a person, not dropped and
    not re-applied by itself."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)
    host.backup(["cam-SN1"], devices["cam-SN0"].disk_door()); agents["cam-SN1"].sync()
    _keep_an_edit_for(fed, devices, "SN2", host.vars, wall)          # after the backup
    devices["cam-SN0"].power_off()
    new, report = rehost(fed, "cam-SN1", offline, DOMAIN, _objects(devices), wall)
    _agent(fed, devices, "cam-SN3", "cam-SN1", wall).sync()          # a member now carries term 2

    devices["cam-SN0"].boot()
    assert host.check() is False
    try:
        host.backup(["cam-SN3"], devices["cam-SN0"].disk_door())
        raise AssertionError("a deposed host must not publish")
    except Deposed as e:
        assert "cam-SN1" in str(e) and "term 2" in str(e)
    left = stranded(fed.clusters["cam-SN0"].vars, report["state"])
    assert [(p, k) for p, k, _ in left] == [("domain/pending/cam-SN2", "SN2")]
    assert json.loads(left[0][2])["fields"]["name"]["new"] == "SN2-renamed"


def test_a_backup_not_signed_by_the_domain_is_ignored_however_new_it_claims_to_be():
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)
    host.backup(["cam-SN1"], devices["cam-SN0"].disk_door()); agents["cam-SN1"].sync()
    forged = sign({"term": 9, "rev": 99, "host": "cam-SN2", "state": {"domain/grants/cam-SN3": {"mallory": "admin"}}},
                  TokenIssuer(DOMAIN))
    devices["cam-SN2"].disk.put(BACKUP, json.dumps(forged).encode())
    devices["cam-SN2"].flash.put(BACKUP, {"rev": 99, "term": 9, "sha256": "x"})
    devices["cam-SN0"].power_off()
    new, report = rehost(fed, "cam-SN1", offline, DOMAIN, _objects(devices), wall)
    assert report["restored_from"] == "cam-SN1" and report["rev"] == 1 and report["term"] == 2
    assert [n for n, _ in report["ignored"]] == ["cam-SN2"]


def test_a_host_restored_without_the_signers_key_is_followed_by_nobody():
    """The signer's key is the one thing never in the backup, and this is why. A host set up from any other
    key finds no backup that verifies, and its claim verifies on no member: every agent stays where it
    was. Losing that key does not lose the site — it loses the ability to move the domain."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)
    host.backup(["cam-SN1"], devices["cam-SN0"].disk_door()); agents["cam-SN1"].sync()
    devices["cam-SN0"].power_off()
    wrong = Signer(DOMAIN, FakeVariables(), now=wall).backup()
    impostor, report = rehost(fed, "cam-SN2", wrong, DOMAIN, _objects(devices), wall)
    assert report["restored_from"] is None and [n for n, _ in report["ignored"]] == ["cam-SN1"]
    keys = signer.tokens.keyset()
    assert find_host(fed, devices["cam-SN3"].flash, keys, wall()) == "cam-SN0"      # still the old host, off, waited for


def test_every_rehost_takes_a_larger_term_than_any_member_has_seen():
    """Twice in a month: SN0 dies, the domain goes to SN1 at term 2; SN1 is stolen, it goes to SN2. The new
    term is one more than the largest ANY reachable member carries — 3, not 2 again — or the host that
    comes back from the first re-host would tie with the second and nothing could tell them apart."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)
    host.backup(["cam-SN1", "cam-SN2"], devices["cam-SN0"].disk_door())
    agents["cam-SN1"].sync(); agents["cam-SN2"].sync()
    devices["cam-SN0"].power_off()
    second, _ = rehost(fed, "cam-SN1", offline, DOMAIN, _objects(devices), wall)
    for name in ("cam-SN2", "cam-SN3"):
        _agent(fed, devices, name, "cam-SN1", wall).sync()
    devices["cam-SN1"].power_off()
    third, report = rehost(fed, "cam-SN2", offline, DOMAIN, _objects(devices), wall)
    assert report["term"] == 3
    _agent(fed, devices, "cam-SN3", "cam-SN2", wall).sync()
    devices["cam-SN1"].boot()
    assert second.check() is False and find_host(fed, devices["cam-SN3"].flash, signer.tokens.keyset(), wall()) == "cam-SN2"


def test_a_planned_handover_strands_nothing():
    """SN0 is alive and being replaced; the operator moves the domain to SN1. The emergency path would work
    and could strand whatever SN0 changed after its last backup. The planned one cannot: SN0 freezes —
    an edit arriving in those seconds is refused with the reason, not accepted into a gap — makes its last
    backup to SN1, SN1's agent takes it, and the re-host restores from a copy that has everything. SN0,
    still reachable, reads the larger term and steps down, and the report says nothing was stranded."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)
    _keep_an_edit_for(fed, devices, "SN3", host.vars, wall)          # kept before the handover: must travel

    view = ReadView(fed, wall=wall)
    view.refresh()
    devices["cam-SN2"].power_off()
    view.refresh()
    api = ConsoleAPI(DomainDirectory(fed, wall=wall), lambda n: devices[n], verifier=lambda t: t,
                     pending=GuardedPending(PendingEdits(host.vars, wall), host), last_known=view.last_known)

    def carry_to():
        try:                                             # the moment an operator edits during the handover
            api.update_camera("SN2", {"name": "late"}, idempotency_key="k-late", token="anna")
            raise AssertionError("a frozen host must refuse the edit")
        except ApiError as e:
            assert e.status == 503 and "handing the domain over to cam-SN1" in e.detail
        agents["cam-SN1"].sync()

    new, report = handover(host, "cam-SN1", offline, DOMAIN, _objects(devices), carry_to, wall)
    assert report["planned"] and report["stranded"] == [] and report["term"] == 2
    assert report["sentence"].endswith("nothing stranded")
    assert "SN3" in PendingEdits(new.vars, wall).of("cam-SN3")
    assert host.deposed_by["host"] == "cam-SN1" and fed.domain_cluster.name == "cam-SN1"


def test_a_handover_the_target_did_not_take_is_called_off_and_changes_nothing():
    """SN1's agent could not take the last backup — SN1 went off in the middle. Re-hosting now would start
    the new term from an older copy, which is the emergency path's loss taken on for no emergency. So the
    handover is called off: SN0 unfreezes and is still the host at term 1, and no member carries anything
    new."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)

    def carry_to():
        devices["cam-SN1"].power_off()
        agents["cam-SN1"].sync()

    try:
        handover(host, "cam-SN1", offline, DOMAIN, _objects(devices), carry_to, wall)
        raise AssertionError("the handover must be called off")
    except RuntimeError as e:
        assert "called off" in str(e)
    assert host.frozen_for is None and host.term == 1 and fed.domain_cluster.name == "cam-SN0"
    host.guard()                                         # writes are accepted again
    keys = signer.tokens.keyset()
    assert read_host(devices["cam-SN3"].flash, keys, wall())["term"] == 1


def test_a_write_that_slips_past_the_freeze_is_reported_not_trusted_away():
    """"Nothing stranded" is checked, not asserted. A path that writes the domain's state without asking the
    host's guard — a bug, a second process — is exactly what the freeze cannot stop, and the report finds it:
    the handover says how many items were stranded, and `stranded` names them."""
    wall = Clock()
    fed, devices, signer, offline, host, agents = _site(wall)

    def carry_to():
        PendingEdits(host.vars, wall).add("cam-SN2", "SN2", {"name": "sneaked"}, {"name": "SN2"}, "anna")   # no guard
        agents["cam-SN1"].sync()

    new, report = handover(host, "cam-SN1", offline, DOMAIN, _objects(devices), carry_to, wall)
    assert [(p, k) for p, k, _ in report["stranded"]] == [("domain/pending/cam-SN2", "SN2")]
    assert "1 item(s) stranded" in report["sentence"]
