"""Settings -> AI -> AI engine: which device runs the AI (CPU / Intel GPU /
NVIDIA GPU), chosen from the web page.

The AI service (ai-worker) is part of every install; the install/update
scripts give it the GPU when the machine has one and record what they saw in
/data/hardware.json. Everything here only *asks* the AI service - list its
devices, time them, use one - so the web app never needs control of Docker.
A change of hardware itself still needs the normal update; this page says so,
in plain words, when that's the missing step.
"""

import json
import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import require_admin
from app.db.session import AsyncSessionLocal
from app.models.ai_settings import AISettings
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/engine", tags=["ai"])

LOCAL_WORKER_URL = "http://ai-worker:8811"  # the AI service on this NVR (compose service name)
HARDWARE_FILE = os.environ.get("LIGHTNVR_HARDWARE_FILE", "/data/hardware.json")


def hardware_report() -> dict | None:
    try:
        with open(HARDWARE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def hints(hw: dict | None, engine: dict | None) -> list[str]:
    """What's in the way of a faster engine, and the one step that fixes it."""
    out: list[str] = []
    if not hw:
        out.append("Run the update once (./scripts/update-linux.sh) so the NVR can check this machine for a GPU.")
        return out
    devices = {d["id"]: d for d in (engine or {}).get("devices", [])}
    vm = hw.get("virt") not in (None, "", "none")
    if hw.get("intel_gpu") and not hw.get("dri"):
        out.append(
            "An Intel GPU is in this machine but its driver isn't loaded, so it can't be used yet. "
            + ("This is a VM: make sure the GPU is passed through, then in the VM run "
               if vm else "Run ")
            + "'sudo apt install linux-modules-extra-$(uname -r) intel-opencl-icd', reboot, and run the update."
        )
    elif hw.get("dri") and devices.get("intel_gpu") and not devices["intel_gpu"]["available"]:
        out.append("An Intel GPU was found but isn't connected to the AI service yet - run the update "
                   "(./scripts/update-linux.sh) and it will be.")
    if hw.get("nvidia_gpu") and not hw.get("nvidia_toolkit"):
        out.append("An NVIDIA GPU is in this machine but Docker can't use it yet: install the NVIDIA driver and the "
                   "NVIDIA Container Toolkit (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/), "
                   "then run the update.")
    elif hw.get("nvidia_gpu") and devices.get("nvidia_gpu") and not devices["nvidia_gpu"]["available"]:
        out.append("An NVIDIA GPU is ready on the host - run the update to switch the AI service to its NVIDIA image.")
    if hw.get("ai_worker") == "off":
        out.append("The AI service is turned off for this NVR (AI_WORKER=off in .env).")
    return out


async def _settings() -> AISettings | None:
    async with AsyncSessionLocal() as db:
        s = await db.get(AISettings, 1)
        if s is not None:
            db.expunge(s)
        return s


def _worker(s: AISettings | None) -> tuple[str, dict, bool]:
    """(url, headers, in_use): the worker the NVR uses, else its own one."""
    if s is not None and s.backend == "remote" and s.remote_url:
        headers = {"X-API-Key": s.remote_api_key} if s.remote_api_key else {}
        return s.remote_url.rstrip("/"), headers, True
    return LOCAL_WORKER_URL, {}, False


async def _call(method: str, url: str, headers: dict, timeout: float, **kw) -> dict:
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        r = await client.request(method, url, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"AI service: {detail}")
    return r.json()


@router.get("")
async def engine(_: User = Depends(require_admin)):
    s = await _settings()
    url, headers, in_use = _worker(s)
    hw = hardware_report()
    state, error = await _engine_of(url, headers)
    local = url == LOCAL_WORKER_URL
    # This NVR's own devices too, so the card can offer them while another
    # PC is in use.
    local_state = state if local else (await _engine_of(LOCAL_WORKER_URL, {}))[0]
    return {
        "worker_url": url, "in_use": in_use, "local": local, "reachable": state is not None,
        "error": error, "engine": state, "local_engine": local_state, "hardware": hw,
        "hints": hints(hw, local_state),
    }


async def _engine_of(url: str, headers: dict) -> tuple[dict | None, str | None]:
    try:
        return await _call("GET", f"{url}/v2/engine", headers, 15), None
    except Exception as exc:
        return None, str(getattr(exc, "detail", exc)) or type(exc).__name__


class DeviceIn(BaseModel):
    device: str  # auto | cpu | intel_gpu | nvidia_gpu


@router.put("")
async def select_device(payload: DeviceIn, user: User = Depends(require_admin)):
    if payload.device not in ("auto", "cpu", "intel_gpu", "nvidia_gpu"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="device: auto|cpu|intel_gpu|nvidia_gpu")
    url, headers, _ = _worker(await _settings())
    # A GPU's first run compiles the model: allow for it.
    state = await _call("POST", f"{url}/v2/engine", headers, 240, data={"device": payload.device})
    from app.services.events import emit_event

    await emit_event(None, "audit", f"AI engine set to {payload.device} by {user.username} "
                                    f"(running on {state.get('active')})", notify=False)
    return state


@router.post("/benchmark")
async def benchmark_devices(_: User = Depends(require_admin)):
    url, headers, _ = _worker(await _settings())
    return await _call("POST", f"{url}/v2/engine/benchmark", headers, 300)


# ── Detection model (ready-made models published by LightNVR) ────────────────


async def _detectors(url: str, headers: dict, method: str = "GET", path: str = "", timeout: float = 20) -> dict:
    from app.vision.models import DETECTORS

    if path and path.split("/")[1] not in DETECTORS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown detection model")
    try:
        return await _call(method, f"{url}/v2/detectors{path}", headers, timeout)
    except HTTPException as exc:
        if "Not Found" in str(exc.detail):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail="This AI service is older than the model choice - update it first.") from exc
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"The AI service isn't answering ({type(exc).__name__}).") from exc


def _with_settings_model(state: dict, s: AISettings | None) -> dict:
    # Detection asks for the NVR's saved model on every request, so that is
    # the one really in use.
    if s is not None and s.detection_model:
        state["in_use"] = s.detection_model
    return state


@router.get("/models")
async def detection_models(_: User = Depends(require_admin)):
    s = await _settings()
    url, headers, _ = _worker(s)
    return _with_settings_model(await _detectors(url, headers), s)


@router.post("/models/{name}/download")
async def download_detection_model(name: str, user: User = Depends(require_admin)):
    s = await _settings()
    url, headers, _ = _worker(s)
    state = await _detectors(url, headers, "POST", f"/{name}/download", timeout=900)
    from app.services.events import emit_event

    await emit_event(None, "audit", f"Detection model {name} downloaded by {user.username}", notify=False)
    return _with_settings_model(state, s)


@router.post("/models/{name}/test")
async def test_detection_model(name: str, _: User = Depends(require_admin)):
    s = await _settings()
    url, headers, _ = _worker(s)
    # A GPU's first run compiles the model: allow for it.
    return _with_settings_model(await _detectors(url, headers, "POST", f"/{name}/test", timeout=300), s)


@router.post("/models/{name}/use")
async def use_detection_model(name: str, user: User = Depends(require_admin)):
    s = await _settings()
    url, headers, _ = _worker(s)
    state = await _detectors(url, headers, "POST", f"/{name}/use", timeout=300)
    async with AsyncSessionLocal() as db:
        record = await db.get(AISettings, 1)
        if record is not None:
            record.detection_model = name
            await db.commit()
    from app.services.events import emit_event

    await emit_event(None, "audit", f"Detection model set to {name} by {user.username}", notify=False)
    return state
