"""Settings -> AI -> Describe what's happening -> This NVR.

The describer (docker-compose.describe.yml: Ollama with a small vision model)
is added by the install/update scripts when this machine can run one, and
idles until it's set up here. This page says what the machine is (from
/data/hardware.json, written by those scripts), which models fit it and how
long a description will take, then downloads the chosen model, times it on
one of your own camera pictures, and switches descriptions to it. Like the AI
engine, everything here only *asks* the describer over the network - the web
app never controls Docker.
"""

import asyncio
import json
import logging
import os
import re
import time
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.routes.ai import _to_out
from app.api.routes.ai_engine import hardware_report
from app.core.deps import require_admin
from app.db.session import AsyncSessionLocal
from app.models.ai_settings import AISettings
from app.models.user import User
from app.services.ai.vlm import LOCAL_DESCRIBER_URL, LOCAL_TIMEOUT_SECONDS, _describe_local, is_local

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/describer", tags=["ai"])

STATE_FILE = os.environ.get("LIGHTNVR_DESCRIBER_FILE", "/data/describer.json")
LICENSE = "Apache-2.0 (Qwen3-VL, by the Qwen team), run with Ollama (MIT)"

# Vision models for a home NVR, smallest first: the *instruct* builds - the
# default Qwen3-VL tags are "thinking" models, which spend a minute of CPU
# reasoning before writing a word. `ram_gb` is what one needs loaded (the
# file plus working memory for a picture), measured with a 4096 context.
MODELS: list[dict] = [
    {"name": "qwen3-vl:2b-instruct", "label": "Small", "size_gb": 1.9, "ram_gb": 3.9, "gpu_only": False,
     "cpu_estimate": "about 1-2 minutes per description on 2 CPU cores",
     "about": "Fits a CPU: a short, plain description of who is there and what they are doing."},
    {"name": "qwen3-vl:4b-instruct", "label": "Better", "size_gb": 3.3, "ram_gb": 5.5, "gpu_only": False,
     "cpu_estimate": "about 2-4 minutes per description on 2 CPU cores - too slow for most summaries",
     "about": "Notices more - what's being carried, clothing, a parcel left behind. About twice as slow."},
    {"name": "qwen3-vl:8b-instruct", "label": "Best", "size_gb": 6.2, "ram_gb": 8.0, "gpu_only": True,
     "cpu_estimate": "",
     "about": "The most detailed. Needs an NVIDIA GPU with at least 8 GB of memory."},
]
BY_NAME = {m["name"]: m for m in MODELS}
SMALL = MODELS[0]["name"]
# A description slower than this can't make it into a summary in time.
TOO_SLOW_SECONDS = 150

_jobs: dict[str, dict] = {}  # model -> the download/test running (or just finished) for it


def _load_results() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("results", {})
    except (OSError, ValueError):
        return {}


def _save_result(name: str, result: dict) -> None:
    results = _load_results()
    results[name] = result
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"results": results}, f)
    except OSError:
        logger.warning("Could not save the describer test result to %s", STATE_FILE)


def mem_limit_mb(text: str | None) -> int:
    """'4500m' / '8g' (compose's mem_limit) -> MB."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)b?\s*", (text or "").lower())
    if not m:
        return 5120
    n, unit = float(m.group(1)), m.group(2)
    return int(n * 1024 if unit == "g" else n / 1024 if unit == "k" else n)


def assess(hw: dict | None, reachable: bool, installed: set[str]) -> dict:
    """What this machine can do: its state, in words, and how each model fits."""
    gpu = bool(hw and hw.get("nvidia_gpu") and hw.get("nvidia_toolkit"))
    room = mem_limit_mb((hw or {}).get("describer_mem_limit"))
    if hw and hw.get("ram_mb"):
        machine = f"{hw.get('cores', '?')} cores, {round(hw['ram_mb'] / 1024)} GB RAM, " + (
            "an NVIDIA GPU it can use" if gpu else "no GPU it can use (Intel graphics can't run these models)")
    else:
        machine = ""
    if reachable:
        state, why = "ready", ""
    elif hw and hw.get("describer") == "too_small":
        state, why = "too_small", f"This machine is too small for descriptions: it {hw.get('describer_why')}."
    elif hw and hw.get("describer") == "off":
        state, why = "off", "Descriptions on this machine are turned off (DESCRIBER=off in .env)."
    else:
        state, why = "not_installed", ("Run the update once (./scripts/update-linux.sh, or update-windows.ps1 on "
                                       "Windows) - it checks this machine and adds the describer if it can run one.")
    models = []
    for m in MODELS:
        if m["gpu_only"] and not gpu:
            fit, note = "no", "needs an NVIDIA GPU"
        elif m["ram_gb"] * 1024 > room:
            fit, note = "no", f"needs about {m['ram_gb']:g} GB; the describer may use {room / 1024:.1f} GB here"
        elif gpu:
            fit, note = "good", "a few seconds per description on the GPU"
        else:
            fit, note = ("good" if m["name"] == SMALL else "slow"), m["cpu_estimate"]
        models.append({**m, "fit": fit, "fit_note": note, "installed": m["name"] in installed})
    fitting = [m["name"] for m in models if m["fit"] != "no"]
    recommended = (fitting[-1] if gpu else SMALL) if fitting else None
    return {"state": state, "why": why, "machine": machine, "gpu": gpu, "models": models,
            "recommended": recommended if recommended in fitting else None}


async def _settings() -> AISettings | None:
    async with AsyncSessionLocal() as db:
        s = await db.get(AISettings, 1)
        if s is not None:
            db.expunge(s)
        return s


async def _local(path: str, method: str = "GET", timeout: float = 8, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method, f"{LOCAL_DESCRIBER_URL}{path}", **kw)


async def _installed() -> tuple[bool, str, set[str]]:
    try:
        version = (await _local("/api/version")).json().get("version", "")
        tags = (await _local("/api/tags")).json().get("models", [])
        return True, version, {t["name"] for t in tags}
    except Exception:
        return False, "", set()


@router.get("")
async def describer(_: User = Depends(require_admin)):
    reachable, version, installed = await _installed()
    out = assess(hardware_report(), reachable, installed)
    results = _load_results()
    for m in out["models"]:
        m["result"] = results.get(m["name"])
        m["job"] = _jobs.get(m["name"])
    s = await _settings()
    out.update(version=version, license=LICENSE,
               in_use=s.vlm_model if s is not None and s.vlm_enabled and is_local(s) else None)
    return out


def _known(name: str) -> None:
    if name not in BY_NAME:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown description model")


def _start(name: str, kind: str, work) -> None:
    job = _jobs.get(name)
    if job and job["status"] == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Already {job['kind']}ing {name}")
    _jobs[name] = {"kind": kind, "status": "running", "progress": 0.0, "message": "", "started": time.time()}

    async def run():
        try:
            await work(_jobs[name])
            _jobs[name]["status"] = "done"
        except Exception as exc:
            logger.warning("Describer %s of %s failed: %s", kind, name, exc)
            _jobs[name].update(status="failed", message=str(exc)[:300] or type(exc).__name__)

    asyncio.get_running_loop().create_task(run())


@router.post("/models/{name:path}/download")
async def download(name: str, user: User = Depends(require_admin)):
    """Fetch the model into the describer (minutes for a few GB): runs in the
    background, the page follows its progress."""
    _known(name)

    async def work(job: dict) -> None:
        layers: dict[str, tuple[int, int]] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=600)) as client:
            async with client.stream("POST", f"{LOCAL_DESCRIBER_URL}/api/pull",
                                     json={"model": name, "stream": True}) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise RuntimeError(msg["error"])
                    if msg.get("digest") and msg.get("total"):
                        layers[msg["digest"]] = (msg.get("completed", 0), msg["total"])
                        done, total = (sum(x) for x in zip(*layers.values()))
                        job["progress"] = round(done / total, 3) if total else 0.0
                    job["message"] = msg.get("status", "")
        job["progress"] = 1.0

    _start(name, "download", work)
    from app.services.events import emit_event

    await emit_event(None, "audit", f"Description model {name} download started by {user.username}", notify=False)
    return {"started": True}


def _mirrored(jpeg: bytes) -> bytes:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    return cv2.imencode(".jpg", cv2.flip(img, 1))[1].tobytes() if img is not None else jpeg


async def _test_picture() -> tuple[bytes, str]:
    """One of this NVR's own pictures: the latest event snapshot, else a
    camera's current frame."""
    from sqlalchemy import select

    from app.models.event import Event
    from app.services.frame_bus import frame_bus

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Event).where(Event.snapshot_path.is_not(None),
                                Event.type.in_(("incident_summary", "motion_summary", "motion")))
            .order_by(Event.id.desc()).limit(20)
        )).scalars().all()
    for ev in rows:
        if ev.snapshot_path and os.path.exists(ev.snapshot_path):
            with open(ev.snapshot_path, "rb") as f:
                return f.read(), f"the picture from \"{ev.message.splitlines()[0][:80]}\""
    from app.models.camera import Camera

    async with AsyncSessionLocal() as db:
        cams = (await db.execute(select(Camera.id, Camera.name))).all()
    for cid, cname in cams:
        jpeg = frame_bus.get_latest(cid)
        if jpeg:
            return jpeg, f"camera '{cname}' right now"
    raise RuntimeError("No camera picture yet to test with - wait until a camera is online")


@router.post("/models/{name:path}/test")
async def test(name: str, _: User = Depends(require_admin)):
    """Describe one of your own pictures, then its mirror image: the first run
    includes loading the model; the second is what every visit will take (a
    new picture - the same one again would come from Ollama's cache)."""
    _known(name)

    async def work(job: dict) -> None:
        jpeg, source = await _test_picture()
        probe = SimpleNamespace(vlm_provider="ollama", vlm_url=LOCAL_DESCRIBER_URL, vlm_model=name)
        prompt = ("This is a picture from a home security camera. Describe in at most two factual sentences "
                  "(40 words) what is happening. No preamble, no speculation beyond the image.")
        job["message"] = "Loading the model and describing a picture…"
        t0 = time.monotonic()
        await _describe_local(probe, jpeg, prompt)
        first = time.monotonic() - t0
        job.update(progress=0.5, message="Describing a second picture to time a normal run…")
        t1 = time.monotonic()
        text = await _describe_local(probe, await asyncio.to_thread(_mirrored, jpeg), prompt)
        took = time.monotonic() - t1
        result = {"ok": bool(text), "seconds": round(took, 1), "first_seconds": round(first, 1), "text": text,
                  "picture": source, "at": time.time()}
        if not text:
            result["error"] = "The model answered with no text"
        elif took > TOO_SLOW_SECONDS:
            result.update(ok=False, error=f"Works, but takes {took:.0f} s here - more than the "
                                          f"{TOO_SLOW_SECONDS} s a summary can wait. Use another PC instead.")
        _save_result(name, result)
        job["progress"] = 1.0

    async def guarded(job: dict) -> None:
        try:
            await work(job)
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"No answer within {LOCAL_TIMEOUT_SECONDS:.0f} s - too slow on this machine for "
                               "summaries. Use another PC instead.") from exc

    _start(name, "test", guarded)
    return {"started": True}


@router.post("/models/{name:path}/delete")
async def delete(name: str, user: User = Depends(require_admin)):
    """Free the disk space a model takes (it can be downloaded again)."""
    _known(name)
    s = await _settings()
    if s is not None and s.vlm_enabled and is_local(s) and s.vlm_model == name:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That model is in use - switch first")
    r = await _local("/api/delete", "DELETE", json={"model": name})
    if r.status_code >= 400 and r.status_code != 404:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Describer: {r.text[:200]}")
    from app.services.events import emit_event

    await emit_event(None, "audit", f"Description model {name} removed by {user.username}", notify=False)
    return {"deleted": name}


class UseIn(BaseModel):
    model: str


@router.post("/use")
async def use(payload: UseIn, user: User = Depends(require_admin)):
    """Descriptions from now on come from this NVR's describer with `model`."""
    _known(payload.model)
    reachable, _, installed = await _installed()
    if not reachable:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="The describer on this NVR isn't running")
    if payload.model not in installed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{payload.model} isn't downloaded yet")
    async with AsyncSessionLocal() as db:
        record = await db.get(AISettings, 1)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI settings not initialised")
        record.vlm_enabled = True
        record.vlm_provider = "ollama"
        record.vlm_url = LOCAL_DESCRIBER_URL
        record.vlm_model = payload.model
        record.vlm_api_key = ""
        record.event_summary_vlm = True
        await db.commit()
        await db.refresh(record)
        out = _to_out(record)
    from app.services.events import emit_event

    await emit_event(None, "audit", f"Descriptions set to {payload.model} on this NVR by {user.username}", notify=False)
    return out
