import asyncio
import logging
from collections import deque

from app.services.events import emit_event

logger = logging.getLogger(__name__)

_proc: asyncio.subprocess.Process | None = None
_supervisor_task: asyncio.Task | None = None
_stopping = False
_last_error: str | None = None
_recent_log: deque[str] = deque(maxlen=20)


def get_status() -> dict:
    if _proc is None or _proc.returncode is not None:
        return {"state": "stopped", "error": _last_error, "log_tail": list(_recent_log)}
    connected = any("Registered tunnel connection" in line for line in _recent_log)
    return {"state": "connected" if connected else "starting", "error": _last_error, "log_tail": list(_recent_log)}


async def _pump_logs(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        _recent_log.append(raw_line.decode(errors="ignore").rstrip())


async def _stop_proc() -> None:
    global _proc
    if _proc and _proc.returncode is None:
        _proc.terminate()
        try:
            await asyncio.wait_for(_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            _proc.kill()
    _proc = None


async def _supervised_run(token: str) -> None:
    """The tunnel ingress mapping (which public hostname points at this app)
    is configured in the Cloudflare Zero Trust dashboard, not here - point
    its origin service at https://nginx:443 with "No TLS Verify" enabled,
    since "nginx" only resolves on this compose network (which is where
    this subprocess runs) and its cert is self-signed by default.
    """
    global _stopping, _proc, _last_error
    backoff = 5
    while not _stopping:
        _recent_log.clear()
        _last_error = None
        try:
            _proc = await asyncio.create_subprocess_exec(
                "cloudflared", "tunnel", "run", "--token", token,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            _last_error = str(exc)
            logger.warning("Failed to start cloudflared: %s", exc)
            await emit_event(None, "system", f"Cloudflare Tunnel failed to start: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        log_task = asyncio.create_task(_pump_logs(_proc))
        await _proc.wait()
        await asyncio.gather(log_task, return_exceptions=True)

        if _stopping:
            break

        _last_error = "\n".join(list(_recent_log)[-3:]) or "exited unexpectedly"
        logger.warning("cloudflared exited unexpectedly: %s", _last_error)
        await emit_event(None, "system", "Cloudflare Tunnel exited unexpectedly and is restarting")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


async def set_enabled(enabled: bool, token: str | None) -> None:
    global _supervisor_task, _stopping

    if _supervisor_task and not _supervisor_task.done():
        _stopping = True
        _supervisor_task.cancel()
        await asyncio.gather(_supervisor_task, return_exceptions=True)
        await _stop_proc()

    if enabled and token:
        _stopping = False
        _supervisor_task = asyncio.create_task(_supervised_run(token))


async def shutdown() -> None:
    global _supervisor_task, _stopping
    _stopping = True
    if _supervisor_task and not _supervisor_task.done():
        _supervisor_task.cancel()
        await asyncio.gather(_supervisor_task, return_exceptions=True)
    await _stop_proc()
