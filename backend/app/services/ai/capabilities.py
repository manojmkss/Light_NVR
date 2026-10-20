"""What can this machine (and any AI worker) actually do?

Read-only probing plus a real benchmark. Presence of a device file proves
nothing - an Intel iGPU passed through to an ESXi VM often shows /dev/dri yet
can't run inference - so feasibility is always decided from measured latency,
never from what hardware merely appears to exist.
"""

import asyncio
import glob
import logging
import os
import platform
import time

import numpy as np

logger = logging.getLogger(__name__)


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _cpu_info() -> dict:
    text = _read("/proc/cpuinfo")
    model = ""
    flags: set[str] = set()
    for line in text.splitlines():
        if line.startswith("model name") and not model:
            model = line.split(":", 1)[1].strip()
        elif line.startswith(("flags", "Features")) and not flags:
            flags = set(line.split(":", 1)[1].split())
    return {
        "model": model or platform.processor() or platform.machine(),
        "arch": platform.machine(),
        "cores": os.cpu_count() or 1,
        "avx2": "avx2" in flags,
        "avx512": any(f.startswith("avx512") for f in flags),
        "vnni": "avx_vnni" in flags or "avx512_vnni" in flags,
    }


def _memory() -> dict:
    out = {}
    try:
        import psutil

        vm = psutil.virtual_memory()
        out = {"total_mb": vm.total // 2**20, "available_mb": vm.available // 2**20}
    except Exception:
        pass
    limit = _read("/sys/fs/cgroup/memory.max").strip()
    if limit and limit != "max":
        try:
            out["container_limit_mb"] = int(limit) // 2**20
        except ValueError:
            pass
    return out


def _devices() -> dict:
    return {
        "intel_or_amd_render_node": bool(glob.glob("/dev/dri/renderD*")),
        "amd_kfd": os.path.exists("/dev/kfd"),
        "nvidia": bool(glob.glob("/dev/nvidia[0-9]*")),
    }


def _ort_providers() -> list[str]:
    try:
        import onnxruntime as ort

        return list(ort.get_available_providers())
    except Exception:
        return []


async def probe(settings=None) -> dict:
    """Everything we can learn without running a model."""
    info = {
        "cpu": _cpu_info(),
        "memory": _memory(),
        "devices": _devices(),
        "onnx_providers": _ort_providers(),
        "worker": None,
    }
    if settings is not None and settings.backend == "remote" and settings.remote_url:
        info["worker"] = await _worker_info(settings)
    return info


async def _worker_info(settings) -> dict:
    import httpx

    headers = {"X-API-Key": settings.remote_api_key} if settings.remote_api_key else {}
    base = settings.remote_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            for path in ("/v2/capabilities", "/health"):
                resp = await client.get(base + path)
                if resp.status_code == 200:
                    return {"reachable": True, "source": path, **resp.json()}
        return {"reachable": False, "error": f"HTTP {resp.status_code}"}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


async def benchmark(backend, runs: int = 12) -> dict:
    """Time real detections on a synthetic substream-sized frame."""
    rng = np.random.default_rng(0)
    frame = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)
    times: list[float] = []
    error = None
    try:
        await backend.detect_frame(frame, 0.5)  # warm-up (model load, first-run allocations)
        for _ in range(runs):
            t0 = time.perf_counter()
            await backend.detect_frame(frame, 0.5)
            times.append(time.perf_counter() - t0)
    except Exception as exc:
        error = str(exc)
    if not times:
        return {"ok": False, "error": error or "no runs"}
    times.sort()
    p50 = times[len(times) // 2]
    p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
    return {"ok": True, "runs": len(times), "p50_ms": round(p50 * 1000, 1), "p95_ms": round(p95 * 1000, 1)}


def feasibility(bench: dict, cameras: int, slots: int = 1, vlm_configured: bool = False, gpu: bool = False) -> dict:
    """Turn a measured latency into recommendations per feature.

    Demand model: ~30% of cameras in motion at once (at least 2), each wanting
    2 checks a second. Capacity: slots / per-frame time."""
    if not bench.get("ok"):
        return {
            "detection": {"level": "unavailable", "reason": bench.get("error", "benchmark failed")},
            "recommended_interval_ms": None,
        }
    per = bench["p50_ms"] / 1000.0
    capacity = slots / per if per > 0 else 0  # frames per second
    busy = max(2, round(0.3 * cameras))
    demand = busy * 2.0
    ratio = capacity / demand if demand else 1.0
    if ratio >= 1.0:
        det = {"level": "recommended", "reason": f"~{capacity:.0f} checks/s available, {demand:.0f}/s needed"}
        interval = 500
    elif ratio >= 0.5:
        det = {"level": "reduced", "reason": f"~{capacity:.1f} checks/s for {busy} busy cameras: check every 1s"}
        interval = 1000
    else:
        det = {"level": "minimal", "reason": f"only ~{capacity:.1f} checks/s: check every 2s, or use a GPU worker"}
        interval = 2000
    spare = max(0.0, 1.0 - 1.0 / ratio) if ratio > 0 else 0.0
    faces = {"level": "recommended" if ratio >= 0.5 else "reduced",
             "reason": "runs only on each person's clearest frames"}
    search = {"level": "recommended" if (gpu or spare >= 0.3) else "not_recommended",
              "reason": "image embeddings on best shots; needs spare capacity on CPU"}
    if vlm_configured:
        vlm = {"level": "recommended" if gpu else "summary_only",
               "reason": "vision model configured" + ("" if gpu else " - on CPU, keep it to summaries")}
    else:
        vlm = {"level": "needs_setup", "reason": "point it at an Ollama/LM Studio box with a GPU"}
    return {
        "detection": det, "faces": faces, "search": search, "descriptions": vlm,
        "recommended_interval_ms": interval, "capacity_fps": round(capacity, 1), "demand_fps": demand,
    }


async def full_report(pipeline, settings, cameras: int) -> dict:
    info = await probe(settings)
    backend = await pipeline._backend_for(settings)
    bench = await benchmark(backend)
    gpu = bool(info.get("worker") and str(info["worker"].get("device", "")).lower() not in ("", "cpu", "fake"))
    info["benchmark"] = bench
    info["feasibility"] = feasibility(
        bench, cameras, slots=getattr(backend, "slots", 1),
        vlm_configured=bool(settings.vlm_enabled and settings.vlm_url), gpu=gpu,
    )
    return info


async def run_blocking(fn, *a):
    return await asyncio.to_thread(fn, *a)
