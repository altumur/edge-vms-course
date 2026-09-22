"""The fifth subsystem: what a scan declares, and which of those declarations are
about having both ends of the work."""
from w2cplatform.spec import Refused
from vms.config import DET_SPEC, DETJOB_SPEC, REC_SPEC


def test_the_scan_has_its_own_budget():
    """The reason a scan is a subsystem and not two fields on a `det` row: a
    retro-search must not be able to spend the streams live detection runs on.
    Separate subsystems means separate heartbeats, so separate capacity numbers
    from the same worker process."""
    assert DETJOB_SPEC.sub.heartbeat_key("d-1") != DET_SPEC.sub.heartbeat_key("d-1")
    assert DETJOB_SPEC.capacity_fallback != DET_SPEC.capacity_fallback
    assert set(DETJOB_SPEC.sub.acl_controller()).isdisjoint(DET_SPEC.sub.acl_controller())


def test_the_scan_writes_into_its_own_tree():
    """A scan of last Tuesday writes events dated last Tuesday. In `det/<unit>/`
    they would be indistinguishable from what the live detector saw then, and a
    second run would double every count."""
    assert DETJOB_SPEC.name == "detjob" and DET_SPEC.name == "det"
    assert DETJOB_SPEC.sub.blobs_prefix() != DET_SPEC.sub.blobs_prefix()
    assert DETJOB_SPEC.sub.snapshot_prefix() != DET_SPEC.sub.snapshot_prefix()


def test_the_recording_is_named_separately_from_the_camera():
    """`rec` is which recording to read, `cam` is which camera the footage is of.
    They were the same string while `rec.subsystem.yaml` said `id: cam`, and this
    test never relied on it — the day a camera got a second archive the recording
    took a name of its own (`7-cloud`), and a job that scans it names THAT, not the
    camera. Nothing here changed: the fields were separate all along."""
    assert REC_SPEC.id == "name"
    assert DETJOB_SPEC.fields["rec"].required and DETJOB_SPEC.fields["cam"].required
    assert DETJOB_SPEC.fields["rec"].name != DETJOB_SPEC.fields["cam"].name


def test_the_interval_is_media_time_and_required():
    """A scan with no ends is a live detector with extra steps. Both ends are
    required, and they are floats because they are unix seconds of MEDIA time —
    not the time the operator pressed the button."""
    for n in ("from", "to"):
        assert DETJOB_SPEC.fields[n].required and DETJOB_SPEC.fields[n].type == "float"


def test_only_what_has_a_reader_above_the_cluster_is_published():
    """М10B Lesson 19's rule: not being a secret is a reason not to hide a field,
    never a reason to publish it. A domain wants to know camera 7 is being scanned;
    it has never wanted the interval, the recording or the disk."""
    assert DETJOB_SPEC.snapshot == ["name", "cam", "kind", "state"]
    for n in ("from", "to", "rec", "home", "params", "mask", "labels"):
        assert n not in DETJOB_SPEC.snapshot


def test_a_job_that_names_no_interval_is_refused_at_the_door():
    DETJOB_SPEC.refuse({"name": "7-lpr-1", "cam": "7", "rec": "7", "kind": "lpr"})     # `refuse` checks shape, not presence
    try:
        DETJOB_SPEC.refuse({"name": "7-lpr-1", "cam": "7", "rec": "7", "kind": "lpr", "started": 1.0})
        raise AssertionError("an unknown field was accepted")
    except Refused:
        pass


def test_the_mask_is_a_digest_here_too():
    """The same rule as `det`: the row names the bytes, the object store holds them."""
    try:
        DETJOB_SPEC.refuse({"name": "j", "cam": "7", "rec": "7", "kind": "lpr", "mask": "x" * 4000})
        raise AssertionError("the bytes were accepted into the row")
    except Refused:
        pass


def test_a_new_job_starts_queued_and_on_a_gpu():
    r = DETJOB_SPEC.row({"id": "7-lpr-1", "name": "7-lpr-1", "cam": "7", "rec": "7", "kind": "lpr", "from": "100", "to": "200"})
    assert r["state"] == "queued" and r["labels"] == ["gpu"] and r["from"] == 100.0 and r["to"] == 200.0
