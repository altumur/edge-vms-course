"""Lesson 9 — the console: one query, positions and reasons on separate
axes, and the recorder's two exported signals.

Operator-owned columns are the ONLY fields any request body can carry.
There is no route that accepts phase, observed_revision, last_seen or
revision, and there is no recorder column: which recorder owns a camera is decided
for the operator, never by them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from worker.reconciler import CONVERGED, LAGGING, STALLED
from console.auth import Sessions, verify_password

UNREACHABLE = "unreachable"


def _credential_free(v: str | None) -> str | None:
    # A secret in a URL is a secret in every log line that URL reaches.
    if v is None:
        return None
    p = urlsplit(v)
    if p.scheme not in ("rtsp", "rtsps") or "@" in p.netloc or not p.netloc:
        raise ValueError("rtsp_url must be rtsp[s]://host[:port]/path with no credentials; "
                         "use cred_username / cred_secret")
    return v


class LoginForm(BaseModel):
    username: str
    password: str


class CameraIn(BaseModel):
    """Operator-owned columns, and nothing else."""
    site_id: Optional[str] = None
    name: str
    rtsp_url: str
    cred_username: Optional[str] = None
    cred_secret: Optional[str] = None
    enabled: bool = True
    retention_days: int = Field(30, ge=1, le=3650)
    priority: int = Field(100, ge=0)

    @field_validator("rtsp_url")
    @classmethod
    def no_credentials_in_url(cls, v):
        return _credential_free(v)


class CameraPatch(BaseModel):
    site_id: Optional[str] = None
    name: Optional[str] = None
    rtsp_url: Optional[str] = None
    cred_username: Optional[str] = None
    cred_secret: Optional[str] = None
    enabled: Optional[bool] = None
    retention_days: Optional[int] = Field(None, ge=1, le=3650)
    priority: Optional[int] = Field(None, ge=0)

    @field_validator("rtsp_url")
    @classmethod
    def no_credentials_in_url(cls, v):
        return _credential_free(v)


def create_app(host) -> FastAPI:
    """`host` is the Worker: the console runs on its loop and reads its store."""
    settings, store = host.settings, host.store
    sessions = Sessions(settings.session_ttl)
    app = FastAPI(title="Recorder console", version="0.10")

    def encrypt(secret: str, cid: int) -> bytes:
        if host.key is None:
            raise HTTPException(503, "no column key on this recorder; cannot store credentials")
        return host.key.encrypt(secret, cid)

    async def current_operator(authorization: str | None = Header(default=None)) -> int:
        tok = authorization.removeprefix("Bearer ").strip() if authorization else None
        oid = sessions.operator_for(tok)
        if oid is None:
            raise HTTPException(401)
        return oid

    @app.post("/login")
    async def login(form: LoginForm):
        row = await store.operator(form.username)
        # Same 401 for unknown user and wrong password: no username oracle.
        if row is None or not verify_password(row["pwhash"], form.password):
            raise HTTPException(401)
        return {"token": sessions.issue(row["id"])}

    # -- operator-owned --------------------------------------------------
    @app.get("/cameras", dependencies=[Depends(current_operator)])
    async def list_cameras():
        return await store.cameras()

    @app.post("/cameras", status_code=201, dependencies=[Depends(current_operator)])
    async def create_camera(cam: CameraIn):
        if cam.site_id:
            await store.upsert_site(cam.site_id, cam.site_id)
        cid = await store.create_camera(cam.model_dump(), encrypt)
        return {"id": cid}

    @app.patch("/cameras/{camera_id}", dependencies=[Depends(current_operator)])
    async def patch_camera(camera_id: int, patch: CameraPatch):
        fields = patch.model_dump(exclude_unset=True)
        if not await store.update_camera(camera_id, fields, encrypt):
            raise HTTPException(404)
        return {"ok": True}

    @app.delete("/cameras/{camera_id}", status_code=204, dependencies=[Depends(current_operator)])
    async def delete_camera(camera_id: int):
        if not await store.delete_camera(camera_id):
            raise HTTPException(404)

    # -- the operator's question -----------------------------------------
    @app.get("/status", dependencies=[Depends(current_operator)])
    async def status():
        return await build_status(store, settings)

    @app.get("/timeline", dependencies=[Depends(current_operator)])
    async def timeline(camera_id: int, start: datetime, end: datetime):
        if end <= start:
            raise HTTPException(422, "end must be after start")
        return await store.timeline(camera_id, start, end, settings.segment_seconds)

    @app.get("/events", dependencies=[Depends(current_operator)])
    async def events(kind: str | None = None, camera_id: int | None = None,
                     limit: int = Query(100, le=1000)):
        return await store.events(kind, camera_id, limit)

    # -- the recorder's two exported signals (М13 scrapes this) --------------
    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        s = await build_status(store, settings)
        return render_metrics(s)

    return app


async def build_status(store, settings) -> dict:
    rows = await store.status()
    conds = await store.conditions()
    now = datetime.now(timezone.utc)
    window = timedelta(seconds=3 * settings.report_interval)
    cameras = []
    for r in rows:
        last_seen = r["last_seen"]
        node_reporting = last_seen is not None and now - last_seen < window
        lag = r["lag"]
        failing = [c for c in conds.get(r["id"], []) if not c["status"]]
        if not node_reporting:
            position = UNREACHABLE                     # grey: we do not know
        elif lag <= 0:
            position = CONVERGED
        elif r["phase"] == "failed" and failing:
            position = STALLED
        else:
            position = LAGGING
        silent = r["silent_for"].total_seconds() if r["silent_for"] is not None else None
        cameras.append({
            "id": r["id"], "name": r["name"], "site_id": r["site_id"], "enabled": r["enabled"],
            "position": position, "lag": lag, "phase": r["phase"],
            "silent_for_seconds": silent,
            "conditions": [{"condition": c["condition"], "status": c["status"],
                            "reason": c["reason"], "since": c["since"]} for c in conds.get(r["id"], [])],
        })
    return {"node_reporting": any(c["position"] != UNREACHABLE for c in cameras) or not cameras,
            "cameras": cameras}


def render_metrics(status: dict) -> str:
    """camera_lag as a DISTRIBUTION, never per camera (a metric is not a
    database); camera_silent_seconds is the one to alarm on."""
    cams = [c for c in status["cameras"] if c["enabled"]]
    lagging = [c for c in cams if c["lag"] > 0]
    stalled = [c for c in cams if c["position"] == STALLED]
    silent = [c["silent_for_seconds"] for c in cams if c["silent_for_seconds"] is not None]
    never = sum(1 for c in cams if c["silent_for_seconds"] is None)
    lines = [
        "# HELP recorder_cameras Enabled cameras on this recorder.",
        "# TYPE recorder_cameras gauge",
        f"recorder_cameras {len(cams)}",
        "# HELP recorder_cameras_lagging Cameras with revision > observed_revision.",
        "# TYPE recorder_cameras_lagging gauge",
        f"recorder_cameras_lagging {len(lagging)}",
        "# HELP recorder_cameras_stalled Cameras lagging with repeated failures.",
        "# TYPE recorder_cameras_stalled gauge",
        f"recorder_cameras_stalled {len(stalled)}",
        "# HELP recorder_camera_lag_max Worst revision lag on this recorder.",
        "# TYPE recorder_camera_lag_max gauge",
        f"recorder_camera_lag_max {max((c['lag'] for c in lagging), default=0)}",
        "# HELP recorder_camera_silent_seconds_max Longest time since any camera wrote a segment. Alarm on this.",
        "# TYPE recorder_camera_silent_seconds_max gauge",
        f"recorder_camera_silent_seconds_max {max(silent, default=0):.0f}",
        "# HELP recorder_cameras_never_recorded Enabled cameras with no segment in the last two hours.",
        "# TYPE recorder_cameras_never_recorded gauge",
        f"recorder_cameras_never_recorded {never}",
        "# HELP recorder_node_reporting 1 if the Worker wrote status within the window.",
        "# TYPE recorder_node_reporting gauge",
        f"recorder_node_reporting {1 if status['node_reporting'] else 0}",
    ]
    return "\n".join(lines) + "\n"
