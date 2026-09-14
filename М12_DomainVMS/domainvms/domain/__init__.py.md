# __init__.py — the `domain` package: М12's statement of purpose, and the import that puts М11's `clustervms` (and М10 through it) on `sys.path`

**Role in the module.** The package docstring is the module's thesis: the smallest layer that can sit above a set of clusters, be switched off, and be the top of the product. It names exactly three things a cluster cannot know — lookup across clusters (`federation.py`), which cluster gets a camera (`placement.py`), whether an answer is complete (`Answer`) — plus the discipline of a layer that may be down: a signer that is the top of its own trust (`signer.py`), identity that never reaches a worker (`identity.py`, `agent.py`), grants that expire per cluster (`grants.py`), a server that joins with nobody typing a secret (`enroll.py`), and a read model that says how old it is (`readview.py`). The code below the docstring does one thing: make `import cluster` work, because every file in this package imports `cluster.variables` / `cluster.objectstore` rather than copying them (the README: "imported, not copied"). Imported first by `tests/conftest.py` (`import domain`) and implicitly by every `python3 -m domain.<x>` entry point.

## Module-level names
- `_here` — the `domainvms/` directory (two levels up from this file).
- `_course` — two levels above that: the course root that holds `М11_ClusterVMS/` beside `М12_DomainVMS/`.
- The `for env, cands in (...)` loop runs once, for `CLUSTERVMS_PATH`. It tries, in order: the env var (if set), `domainvms/clustervms` (a copy or symlink placed beside this package, e.g. in a container image), then `<course>/М11_ClusterVMS/clustervms`. The first existing directory not already on `sys.path` is appended and the search stops. The comment carries the chain: `clustervms/cluster/__init__` then finds `vmsserver` itself (see `../../../М11_ClusterVMS/clustervms/cluster/__init__.py.md`), so М10 is reached through М11 and nothing here names `vmsserver`.
- `import cluster` — executed for its side effect (`noqa: F401`): if no candidate was found this line is the `ModuleNotFoundError` a user sees, which is the intended failure mode ("set `CLUSTERVMS_PATH`").

## Notes
- `sys.path.append`, not `insert(0, …)`: a `cluster` package already importable (an installed one) wins over the course tree.
- `cloud._worker_jobspec()` repeats the same three-candidate search on its own to find `deploy/vmsworker.nomad.hcl`, because it needs the file, not the package.
