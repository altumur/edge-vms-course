"""Lesson 10 — what runs on М9's box: the three processes and the policy pass as
Quadlet units over one image, on the data partition. No podman here (the
generator's dry-run is deploy/check-quadlet.sh, for the bench); this checks
that the units and the Containerfile agree with the package they run."""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(HERE, "deploy")


def unit(name):
    """A unit file as {section: {key: value or [values]}} — systemd keys repeat (Volume=, Environment=), configparser's do not."""
    out, sec = {}, None
    for line in open(os.path.join(DEPLOY, name)):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            sec = line.strip("[]"); out[sec] = {}
        else:
            k, v = line.split("=", 1)
            out[sec].setdefault(k, []).append(v)
    return {sec: {k: (v[0] if len(v) == 1 else v) for k, v in kv.items()} for sec, kv in out.items()}


def test_the_units_run_the_entrypoints_the_package_has():
    from vms import __main__ as m  # noqa: F401  (imports the module without running it: no __name__ == "__main__")
    entrypoints = set(re.findall(r'"(\w+)": \w+', open(os.path.join(HERE, "vms", "__main__.py")).read().split("__main__")[-1]))
    assert entrypoints == {"worker", "controller", "recorder", "reccontroller", "console", "resource", "gateway",
                           "livecontroller", "detworker", "detcontroller", "detjobworker", "detjobcontroller"}
    for name, entry in [("vmsworker@.container", "worker"), ("vmscontroller.container", "controller"),
                        ("console.container", "console"), ("resource.container", "resource"),
                        ("recworker@.container", "recorder"), ("reccontroller.container", "reccontroller"),
                        ("liveworker@.container", "gateway"), ("livecontroller.container", "livecontroller"),
                        ("detworker@.container", "detworker"), ("detcontroller.container", "detcontroller"),
                        ("detjobworker@.container", "detjobworker"), ("detjobcontroller.container", "detjobcontroller")]:
        u = unit(name)
        assert u["Container"]["Image"] == "localhost/vmsserver:latest"                 # one image, one thing to publish
        assert u["Container"]["Exec"] == f"python3 -m vms {entry}"
        assert u["Container"]["EnvironmentFile"] == "/data/config/vms.env"             # the data partition, never a rootfs slot
        for vol in (u["Container"]["Volume"] if isinstance(u["Container"]["Volume"], list) else [u["Container"]["Volume"]]):
            assert vol.startswith("/data/") or vol.startswith("/run/vms:"), vol           # /run/vms: the shared-memory sockets — a tmpfs, not state


def test_who_may_write_where_is_in_the_mounts_too():
    """The ACL says which rows each token writes; the mounts say which bytes.
    The controller has no archive at all; the console cannot write the spool."""
    vols = lambda n: dict(v.split(":", 1) for v in (lambda x: x if isinstance(x, list) else [x])(unit(n)["Container"]["Volume"]))
    assert "/data/archive" not in vols("vmscontroller.container") and "/data/spool" not in vols("vmscontroller.container")
    assert vols("console.container")["/data/spool"].endswith(":ro,z")                # reads, never records
    assert "/data/spool" not in vols("vmsworker@.container")                            # the worker records nothing: no spool
    assert vols("vmsworker@.container")["/data/archive"] == "/data/archive:z"          # its events, vms/<cam>/, on this box's resource
    assert vols("vmsworker@.container")["/data/media"].endswith(":ro,z")
    assert vols("recworker@.container")["/data/spool"] == "/data/spool:z"            # the recorder is the only writer of segments
    assert vols("recworker@.container")["/data/archive"] == "/data/archive:z"
    assert "/data/media" not in vols("recworker@.container")                          # it never reads a camera: it subscribes to the fan-out
    assert vols("vmsworker@.container")["/run/vms"] == "/run/vms:z" == vols("recworker@.container")["/run/vms"]   # the tee's shared memory: written by the worker, read by the recorder beside it
    assert "/data/archive" not in vols("reccontroller.container")
    assert vols("resource.container")["/data/platform"] == "/data/platform:z"       # the heartbeat is written; rows are only read
    assert vols("resource.container")["/data/spool"].endswith(":ro,z")               # the resource never records
    assert unit("recworker@.container")["Container"]["StopTimeout"] == "20"          # SIGTERM finalizes the open segment
    assert unit("resource.container")["Service"]["Restart"] == "always"             # a process, not a timer: the database lives in it


def test_the_image_carries_the_three_packages_and_nothing_else():
    cf = "\n".join(l for l in open(os.path.join(DEPLOY, "Containerfile")) if not l.startswith("#"))   # the instructions, not the notes
    copied = re.findall(r"^COPY (\S+) ", cf, re.M)
    assert copied == ["w2cplatform", "vms", "gstvms"]
    assert "postgres" not in cf.lower()                                                # the per-box database is gone (М10 Lesson 1)
    assert 'CMD ["python3", "-m", "vms", "worker"]' in cf
    env = open(os.path.join(DEPLOY, "vms.env.example")).read()
    assert all(k in env for k in ("PLATFORM_DIR=/data/platform", "SPOOL=/data/spool", "ARCHIVE=/data/archive", "CAPACITY="))
