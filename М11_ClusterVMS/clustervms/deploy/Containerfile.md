# Containerfile — `localhost/clustervms:latest`: М10's image plus the `cluster` package, six entrypoints

**Role.** The one image every Nomad job in `deploy/*.nomad.hcl` runs. It starts `FROM` М10's `localhost/vmsserver:latest` (see `../../../М10_ServerVMS/vmsserver/deploy/Containerfile.md`: Debian, Python, GStreamer, `psimplatform/`, `vms/`, `gstvms/` under `/app`), so a Quadlet unit on the М9 box and a job on the cluster differ only in who starts the process. The header comment gives the build order — М10's image first, then this one from the `clustervms/` module directory — and the four verbs: `python3 -m cluster worker | controller | console | resource`. Built on each server (the jobspecs reference a `localhost/` image, so there is no registry; Podman pulls nothing).

## Stanza by stanza

### Instructions
- `FROM localhost/vmsserver:latest` — М10's image as the base; must exist locally on every Nomad client that can run these jobs.
- `WORKDIR /app` — the same working directory as М10, where `vms/`, `psimplatform/` and `gstvms/` already are.
- `COPY cluster cluster` — this package, to `/app/cluster`. Only `cluster/`; tests and deploy files stay out of the image.
- `ENV VMSSERVER_PATH=/app PYTHONPATH=/app` — `VMSSERVER_PATH` is the first candidate `cluster/__init__.py` tries when it puts М10's package on `sys.path` (see `../cluster/__init__.py.md`); `PYTHONPATH=/app` makes `-m cluster` resolvable from any cwd. Both point at the same directory because М10's modules live directly under `/app`.
- `CMD ["python3", "-m", "cluster", "worker"]` — the default verb; every jobspec overrides it with `args = [...]`, so the CMD only matters for `podman run` by hand.

## Notes
- No `EXPOSE`, no user, no volumes: the jobspecs supply `network_mode = "host"` and the `/data/*` volume binds.
- The image carries `curl` (installed by М10's Containerfile), which `verify-bench.sh` item 5 relies on inside a `vmsworker` allocation.
