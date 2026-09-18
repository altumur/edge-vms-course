"""A device needs a login and a password; the platform's job is to let exactly
two parties see the second one — the operator who writes it and the worker that
opens the camera — and nobody in between."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from vms.archive import ArchiveResource
from vms.config import SPEC
from vms.console import serve
from vms.controller import VmsController
from w2cplatform.secrets import SECRET_MASK, is_secret_field, mask_secrets
from w2cplatform.spec import Refused, SubsystemSpec
from tests.conftest import Box


def test_the_rule_is_a_suffix_and_masking_is_a_copy():
    """One suffix, declared once, and every subsystem has it — the same move `type: int` is.

    A registry of secret field names would be a second place to keep in step with the specs, and the
    specs would win."""
    assert is_secret_field("cred_secret") and not is_secret_field("cred_username")
    assert not is_secret_field("secret_note")            # the suffix is the rule, not the word

    rows = [{"id": "7", "cred_username": "admin", "cred_secret": "Hunter2"}, {"id": "8", "cred_secret": ""}]
    out = mask_secrets(rows)
    assert out[0]["cred_secret"] == SECRET_MASK and out[0]["cred_username"] == "admin"
    assert out[1]["cred_secret"] == ""                   # empty stays empty: a page must tell "not set" from "set"
    assert rows[0]["cred_secret"] == "Hunter2"           # a copy — the caller still holds the row a worker will read


def test_a_spec_may_not_put_a_secret_in_the_snapshot():
    """`vms/snapshot` is what leaves the cluster for М12's directory. So this is refused at LOAD time,
    which is a different thing from being watched for at review time: a subsystem written a year from now
    cannot make the mistake, and nobody has to remember the rule to be protected by it."""
    base = {"name": "x", "unit": {"fields": {"host": {"type": "string"}, "api_secret": {"type": "string"}}}}
    try:
        SubsystemSpec.from_dict({**base, "snapshot": ["host", "api_secret"]})
        raise AssertionError("a secret was accepted into the snapshot")
    except ValueError as e:
        assert "may not be in the snapshot" in str(e)

    # …and the DEFAULT snapshot — "every field" — leaves secrets out rather than refusing: a spec that
    # said nothing made no mistake, and the safe reading of silence is the one that keeps the secret in.
    spec = SubsystemSpec.from_dict(base)
    assert spec.snapshot == ["host"]
    # a snapshot naming a field that does not exist is a typo, and typos in this list are expensive
    try:
        SubsystemSpec.from_dict({**base, "snapshot": ["hsot"]})
        raise AssertionError("accepted a snapshot naming no field")
    except ValueError as e:
        assert "names no field" in str(e)


def test_a_url_field_refuses_a_login():
    """The quiet path a password takes into a system that thinks it has none.

    `source` is `type: url` and `source` is in the snapshot. A userinfo pasted in here would reach М12's
    directory and every console screen, past a mask that only looks at `*_secret`. Refused — not moved
    quietly into the credential fields, because an operator who pasted a browser URL should learn that
    this system keeps the two apart."""
    box = Box()
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    for bad in ("driverpack://root:hunter2@10.0.0.7/cam", "driverpack://admin@10.0.0.7/cam"):
        try:
            con.create_camera({"name": "gate", "source": bad})
            raise AssertionError(f"a credential rode in on the URL: {bad}")
        except Refused as e:
            assert "may not carry a login" in str(e)
    assert con.cameras() == []                                   # nothing was created on the way to the refusal

    cam = con.create_camera({"name": "gate", "source": "driverpack://file/gate.mp4"})
    try:                                                          # and an edit is the same door
        con.update_camera(cam["id"], {"source": "driverpack://root:hunter2@10.0.0.7/cam"})
        raise AssertionError("an update let a credential in")
    except Refused:
        pass


def test_the_console_never_hands_out_the_secret_and_the_store_holds_one_copy():
    """The three ways a row leaves the console, and the one that is easy to miss.

    A create's reply is what the Idempotency-Key store keeps in order to answer a retry the same way. An
    unmasked reply therefore writes a SECOND copy of the secret into the config store, under a key nobody
    would think to look at — so the last thing this test does is walk the whole store."""
    secret = "Hunter2-not-in-any-reply"
    box = Box()
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    srv = serve(con, ArchiveResource(box.spool, box.archive), port=0, wall=box.wall)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        body = json.dumps({"name": "gate", "source": "driverpack://acme/10.0.0.5",
                           "cred_username": "admin", "cred_secret": secret}).encode()
        req = urllib.request.Request(f"{base}/cameras", data=body, method="POST", headers={"Idempotency-Key": "c1"})
        raw = urllib.request.urlopen(req).read()
        r = json.loads(raw)
        assert secret not in raw.decode() and r["cred_secret"] == SECRET_MASK and r["cred_username"] == "admin"

        again = urllib.request.urlopen(req).read()                # the retry is answered from the store
        assert secret not in again.decode(), "the retry replayed the secret"

        upd = urllib.request.Request(f"{base}/cameras/1", data=json.dumps({"cred_secret": secret + "-2"}).encode(),
                                     method="PUT", headers={"Idempotency-Key": "c2"})
        assert secret not in urllib.request.urlopen(upd).read().decode()
        assert secret not in urllib.request.urlopen(f"{base}/cameras").read().decode()
    finally:
        srv.shutdown()

    # The row itself still holds it: the worker has to read it to open the camera.
    assert con.camera(1)["cred_secret"] == secret + "-2"
    # …and that row is the ONLY place in the store that does.
    for path in box.vars.list(""):
        items, _ = box.vars.get(path)
        for k, v in (items or {}).items():
            if secret in str(v):
                assert path == "vms/cameras/1" and k == "cred_secret", f"the secret is also stored at {path}[{k}]"


def test_the_login_leaves_the_cluster_and_the_secret_does_not():
    """The asymmetry, stated where it is enforced: in the YAML, not in the code that publishes."""
    assert "cred_username" in SPEC.snapshot and "cred_secret" not in SPEC.snapshot
    box = Box()
    ctl = VmsController(box.vars.as_writer("vmscontroller", SPEC.acl_controller()), box.objects, wall=box.wall)
    con = VmsController(box.vars.as_writer("console", SPEC.acl_console()), box.objects, wall=box.wall)
    con.create_camera({"name": "gate", "source": "driverpack://acme/10.0.0.5",
                       "cred_username": "admin", "cred_secret": "Hunter2"})
    ctl.publish_snapshot()
    snap = json.loads(box.objects.get("vms/snapshot"))
    row = snap["cameras"][0] if isinstance(snap, dict) and "cameras" in snap else snap[0]
    assert row["cred_username"] == "admin"
    assert "cred_secret" not in row and "Hunter2" not in json.dumps(snap)
