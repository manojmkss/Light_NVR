"""Self-healing and watchdog behaviour."""

import asyncio
import time
from types import SimpleNamespace

from app.core import watchdog as wd_mod
from app.core.log_buffer import scrub_credentials
from app.services import camera_supervisor as sup_mod


def test_tokens_and_rtsp_credentials_are_scrubbed_from_logs():
    line = 'GET /api/cameras/1/stream.mjpeg?quality=sub&token=eyJabc.def HTTP/1.1 rtsp://admin:secret@10.0.0.5/s'
    out = scrub_credentials(line)
    assert "eyJabc" not in out and "secret" not in out and "token=***" in out


def test_watchdog_exits_when_loop_stalls(monkeypatch):
    exits = []
    monkeypatch.setattr(wd_mod, "LOOP_STALL_SECONDS", 0.3)
    monkeypatch.setattr(wd_mod, "HEARTBEAT_SECONDS", 0.05)
    w = wd_mod.Watchdog()
    monkeypatch.setattr(w, "_die", lambda code, why: exits.append(code) or w.stop())

    async def go():
        w.start(asyncio.get_running_loop())
        await asyncio.sleep(0.3)  # healthy: heartbeats flow
        assert exits == []
        time.sleep(0.8)  # block the event loop
        await asyncio.sleep(0.1)

    asyncio.run(go())
    assert exits == [70]


class FakeWorker:
    def __init__(self, cid, healthy):
        self.camera_id, self.name, self._healthy = cid, f"cam{cid}", healthy
        self.stopped = False

    def health(self):
        return (True, "ok") if self._healthy else (False, "live-stream thread stopped")

    async def stop(self):
        self.stopped = True


def test_supervisor_restarts_unhealthy_worker_with_backoff(monkeypatch):
    sup = sup_mod.CameraSupervisor()
    healed = []

    async def heal(cid, worker, reason, attempt):
        healed.append((cid, attempt, reason))

    monkeypatch.setattr(sup, "_self_heal", heal)
    sup._workers = {1: FakeWorker(1, True), 2: FakeWorker(2, False)}

    async def go():
        await sup._check_all()          # restart #1 for camera 2
        await sup._check_all()          # still unhealthy, but inside the backoff window: no restart
        count, last = sup._restarts[2]
        sup._restarts[2] = (count, last - sup_mod.FIRST_RESTART_DELAY * 2 - 1)  # time passes
        await sup._check_all()          # restart #2

    asyncio.run(go())
    assert healed == [(2, 1, "live-stream thread stopped"), (2, 2, "live-stream thread stopped")]
    assert 1 not in sup._restarts


def test_one_camera_failing_to_start_does_not_stop_the_others(monkeypatch):
    sup = sup_mod.CameraSupervisor()
    started = []

    class Cam(SimpleNamespace):
        pass

    async def fake_start(camera):
        if camera.id == 1:
            raise RuntimeError("bad config")
        started.append(camera.id)
        sup._workers[camera.id] = FakeWorker(camera.id, True)

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [Cam(id=1, name="a", enabled=True), Cam(id=2, name="b", enabled=True)]

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *_a):
            return FakeResult()

    monkeypatch.setattr(sup_mod, "AsyncSessionLocal", lambda: FakeDB())
    monkeypatch.setattr(sup, "_start_worker", fake_start)

    async def go():
        await sup.start_all()
        sup._health_task.cancel()

    asyncio.run(go())
    assert started == [2]
