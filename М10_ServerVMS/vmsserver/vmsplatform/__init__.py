"""The platform, on one box (the package is `vmsplatform` only because Python owns the name `platform`). Everything here would host any fleet of
stateless shards writing bulk data; nothing here knows what a camera is.

    variables.py   a small, consistent config store with ModifyIndex and check-and-set (file-backed)
    objects.py     an object store (a directory)
    epoch.py       the fencing-token issuer and the lease, generic
    contract.py    what a subsystem gives the platform: a controller and its workers
    spec.py        the controller as data: SubsystemSpec from <sub>.subsystem.yaml, SpecController
    console.py     the console as data: SpecConsole over the same spec; console.html, the one page
    events.py, eventindex.py, resource.py   buckets, the index over them, the resource job

М11 replaces variables.py with Nomad Variables and objects.py with MinIO,
behind the same interfaces, and changes nothing above this line.
"""
