"""The VMS — the first subsystem the platform hosts.

    reconciler.py   М9 Lesson 6's loop, unchanged: desired persisted, actual derived
    archive.py      the archive as a resource: spool → promote → manifest; retention as a policy
    worker.py       vmsworker — DriverPack as the worker: N pipelines against an assignment
    controller.py   vmscontroller — the only writer of vms/*: cameras, assignment, placement
    console.py      the one-box console: the platform's SpecConsole over the VMS spec, plus /timeline and /segment

Nothing here imports from platform/ except through its public interfaces,
and nothing in platform/ imports from here.
"""
