import asyncio
import json
import logging
import os

from app.services.events import emit_event

logger = logging.getLogger(__name__)

STATE_DIR = "/data/tailscale"
SOCKET_PATH = f"{STATE_DIR}/tailscaled.sock"

_daemon_proc: asyncio.subprocess.Process | None = None
_supervisor_task: asyncio.Task | None = None
_stopping = False
_last_error: str | None = None


async def _run_cli(*args: str, timeout: float = 30) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        "tailscale", "--socket", SOCKET_PATH, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "Command timed out"
    output = stdout.decode(errors="ignore").strip()
    return proc.returncode == 0, output


async def get_status() -> dict:
    if _daemon_proc is None or _daemon_proc.returncode is not None:
        return {"state": "stopped", "ip": None, "hostname": None, "error": _last_error}

    ok, output = await _run_cli("status", "--json", timeout=10)
    if not ok:
        return {"state": "starting", "ip": None, "hostname": None, "error": _last_error}

    try:
        data = json.loads(output)
    except ValueError:
        return {"state": "starting", "ip": None, "hostname": None, "error": _last_error}

    backend_state = data.get("BackendState", "Unknown")
    self_node = data.get("Self") or {}
    ips = self_node.get("TailscaleIPs") or []
    dns_name = (self_node.get("DNSName") or "").rstrip(".")

    if backend_state == "Running":
        state = "connected"
    elif backend_state == "NeedsLogin":
        state = "needs_login"
    else:
        state = "starting"

    return {"state": state, "ip": ips[0] if ips else None, "hostname": dns_name or None, "error": _last_error}


async def _start_daemon() -> None:
    global _daemon_proc
    if _daemon_proc is not None and _daemon_proc.returncode is None:
        return  # already running - tailscaled can outlive a failed `tailscale up` retry
    os.makedirs(STATE_DIR, exist_ok=True)
    _daemon_proc = await asyncio.create_subprocess_exec(
        "tailscaled",
        "--state", f"{STATE_DIR}/tailscaled.state",
        "--socket", SOCKET_PATH,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(2)  # let the daemon create its control socket before the CLI talks to it


async def _connect(authkey: str, hostname: str) -> tuple[bool, str]:
    global _last_error
    await _start_daemon()

    ok, output = await _run_cli("up", f"--authkey={authkey}", f"--hostname={hostname}", "--accept-dns=true", timeout=60)
    if not ok:
        _last_error = output[-500:]
        return False, _last_error

    # Forwards the tailnet's automatically-issued, browser-trusted HTTPS cert
    # straight to nginx over the docker-internal network - "nginx" only
    # resolves from inside this compose network, which is exactly where this
    # subprocess runs; +insecure tolerates nginx's bundled self-signed cert
    # without it needing to be CA-signed. --bg applies the config and exits
    # immediately - newer tailscale CLI versions run serve in the foreground
    # (streaming logs) without it, which would just hang until our own
    # timeout killed it and got misreported as a failure.
    ok2, output2 = await _run_cli("serve", "--bg", "https+insecure://nginx:443", timeout=30)
    if not ok2:
        _last_error = output2[-500:]
        return False, _last_error

    _last_error = None
    return True, "Connected"


async def _stop_daemon() -> None:
    global _daemon_proc
    await _run_cli("down", timeout=15)
    if _daemon_proc and _daemon_proc.returncode is None:
        _daemon_proc.terminate()
        try:
            await asyncio.wait_for(_daemon_proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            _daemon_proc.kill()
    _daemon_proc = None


async def _supervised_run(authkey: str, hostname: str) -> None:
    """Nothing else watches this fire-and-forget task, so a tailscaled crash
    must not be able to silently and permanently disconnect remote access -
    same lesson as the camera worker tasks.
    """
    global _stopping
    backoff = 5
    while not _stopping:
        success, message = await _connect(authkey, hostname)
        if not success:
            logger.warning("Tailscale connect failed: %s", message)
            await emit_event(None, "system", f"Tailscale connection failed: {message}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        backoff = 5
        proc = _daemon_proc
        if proc:
            await proc.wait()
        if _stopping:
            break
        logger.warning("tailscaled exited unexpectedly - reconnecting")
        await emit_event(None, "system", "Tailscale daemon exited unexpectedly and is reconnecting")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


async def set_enabled(enabled: bool, authkey: str | None, hostname: str) -> None:
    """Starts or stops the supervised connection. Called both from the API
    when a setting is saved and at app boot to resume a previously-enabled
    connection - either way, this is the single place that owns the
    background task's lifecycle.
    """
    global _supervisor_task, _stopping

    if _supervisor_task and not _supervisor_task.done():
        _stopping = True
        _supervisor_task.cancel()
        await asyncio.gather(_supervisor_task, return_exceptions=True)
        await _stop_daemon()

    if enabled and authkey:
        _stopping = False
        _supervisor_task = asyncio.create_task(_supervised_run(authkey, hostname))


async def shutdown() -> None:
    global _supervisor_task, _stopping
    _stopping = True
    if _supervisor_task and not _supervisor_task.done():
        _supervisor_task.cancel()
        await asyncio.gather(_supervisor_task, return_exceptions=True)
    await _stop_daemon()
