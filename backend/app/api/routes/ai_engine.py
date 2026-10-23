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
    try:
        state = await _call("GET", f"{url}/v2/engine", headers, 15)
        error = None
    except Exception as exc:
        state, error = None, str(getattr(exc, "detail", exc)) or type(exc).__name__
    return {
        "worker_url": url, "in_use": in_use, "local": url == LOCAL_WORKER_URL, "reachable": state is not None,
        "error": error, "engine": state, "hardware": hw, "hints": hints(hw, state),
    }


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
