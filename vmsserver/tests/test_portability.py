"""The platform runs where the operator's box is, and on Windows that box is
not a Unix one. Both ports carry the same two seams; this is the Python half."""
from __future__ import annotations

import builtins
import importlib
import os
import sys

UNIX_ONLY = ("fcntl", "termios", "grp", "pwd", "posix", "resource")


def _reimport_without(names: set[str], *modules: str) -> list[str]:
    """Import each module with `names` unimportable — what Windows looks like from here.

    Nothing is mocked and nothing is patched inside the modules: the import machinery is told the truth
    about a platform where those modules do not exist, and the package either survives it or does not."""
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        # `level` matters: `from .resource import x` inside the package arrives here as the bare name
        # "resource" with level=1, and the platform HAS a module by that name. Only an absolute import
        # (level 0) can be the standard library's Unix-only one.
        if level == 0 and name.split(".")[0] in names:
            raise ImportError(f"no module named {name!r} on this platform")
        return real_import(name, globals, locals, fromlist, level)

    broken = []
    saved = dict(sys.modules)
    builtins.__import__ = guarded
    try:
        for m in modules:
            sys.modules.pop(m, None)
            try:
                importlib.import_module(m)
            except ImportError as e:
                broken.append(f"{m}: {e}")
    finally:
        builtins.__import__ = real_import
        sys.modules.clear()
        sys.modules.update(saved)
    return broken


def test_the_platform_imports_on_a_box_without_the_unix_modules():
    """The failure this catches is not a wrong answer — it is no answer at all.

    `import fcntl` at the top of `variables.py` did not make a call fail on Windows: the MODULE did not
    import, so nothing that touches Variables existed, and every process died before its first line of
    work. A test that calls functions would never see it, because there would be no functions to call.
    So this test imports, and imports is all it does."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mods = []
    for f in sorted(os.listdir(os.path.join(here, "w2cplatform"))):
        if f.endswith(".py") and not f.startswith("_"):
            mods.append("w2cplatform." + f[:-3])
    assert "w2cplatform.variables" in mods and "w2cplatform.resource" in mods
    broken = _reimport_without(set(UNIX_ONLY), *mods)
    assert not broken, "the platform does not import where these modules are absent:\n  " + "\n  ".join(broken)


def test_the_disk_probe_answers_without_statvfs():
    """`os.statvfs` is Unix-only; `shutil.disk_usage` is not, and it keeps the meaning.

    POSIX `free` is `f_bavail * f_frsize` — what is ours to spend rather than what exists — and on Windows
    it is GetDiskFreeSpaceExW's "available to the caller", which is the same idea. The watermark above it
    needs no `if`, which is the whole point of replacing the call rather than branching around it."""
    from w2cplatform.resource import disk_space
    total, free = disk_space(os.path.dirname(os.path.abspath(__file__)))
    assert total > 0 and 0 <= free <= total
    import w2cplatform.resource as r
    assert "os.statvfs(" not in open(r.__file__, encoding="utf-8").read(), "a Unix-only call came back"


def test_the_lock_is_one_seam_and_the_caller_does_not_branch():
    """Whatever the OS, the caller says `with self._locked():` and nothing else.

    A portability seam that leaks into its callers is not a seam: it is the same `if` repeated at every
    site, and the day a third platform appears, one of them is missed."""
    import inspect

    from w2cplatform.variables import FileVariables, _lock_exclusive
    src = inspect.getsource(FileVariables._locked)
    assert "_lock_exclusive(f)" in src
    for word in ("fcntl", "msvcrt", "win32", "sys.platform", "os.name"):
        assert word not in src, f"{word!r} leaked into the caller"
    assert "fcntl" in inspect.getsource(_lock_exclusive) and "msvcrt" in inspect.getsource(_lock_exclusive)
