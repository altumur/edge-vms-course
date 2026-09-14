"""The VMS — the first subsystem the platform hosts.

    reconciler.py   М9 Lesson 6's loop, unchanged: desired persisted, actual derived
    archive.py      the archive as a resource: spool → promote → manifest; retention as a policy
    worker.py       vmsworker — DriverPack as the worker: N pipelines against an assignment
    controller.py   vmscontroller — the only writer of vms/*: cameras, assignment, placement
    console.py      the one-box console: the platform's SpecConsole over the VMS spec, plus /timeline, /segment and the WHEP door
    gateway.py      the live gateway — the third subsystem's worker: a camera's fan-out as the unit, viewers as the capacity
    live.subsystem.yaml   the third subsystem, as a spec

Nothing here imports from platform/ except through its public interfaces,
and nothing in platform/ imports from here.
"""
# ================================================================================================
# NOTES — what every part of this file does and why (kept beside the code, not in a separate document)
# ================================================================================================
# # __init__.py — the package docstring: what `vms` is, the first subsystem the platform hosts
#
# **Role in the module.** No code, only the docstring. It names the VMS as the *first subsystem* — the thing
# the platform (`psimplatform/`) was built to host without knowing what it is — and gives a one-line map of
# the package: `reconciler.py` (М9 Lesson 6's loop, unchanged: desired persisted, actual derived),
# `archive.py` (the archive as a resource: spool → promote → manifest; retention as a policy), `worker.py`
# (vmsworker — DriverPack as the worker, N pipelines against an assignment), `controller.py` (vmscontroller
# — the only writer of `vms/*`: cameras, assignment, placement) and `console.py` (the one-box console: the
# platform's `SpecConsole` over the VMS spec, plus `/timeline` and `/segment`).
#
# The last sentence fixes the dependency direction that
# `tests/test_lesson1_platform.py::test_the_platform_knows_nothing_about_video` enforces: `vms/` imports
# from the platform only through its public interfaces (`Worker`, `SpecController`, `SpecConsole`,
# `EventLog`, the store Protocols), and nothing in the platform imports from here.
#
# ## Module-level names
# None, and no imports: `import vms` pulls in nothing. `python3 -m vms …` runs `__main__.py`, which imports
# the modules it needs explicitly.
#
# ## Notes
# - The docstring still says `platform/` where the directory is `psimplatform/` (renamed because Python's
#   standard library owns `platform`; see `psimplatform/__init__.py`).
# - `config.py` and `vms.subsystem.yaml` are not listed in the docstring's map; they are the schema the
#   controller and console run from (see `config.py`, `vms.subsystem.yaml`).
# ================================================================================================
