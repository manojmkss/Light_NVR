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


def test_a_camera_back_from_a_long_outage_is_not_called_stalled():
    import time as _time
    from types import SimpleNamespace

    from app.services.camera_worker import CameraWorker

    now = _time.monotonic()
    viewer = SimpleNamespace(thread_alive=True, last_activity=now, last_frame_at=now, connected_at=now - 30)
    rec = SimpleNamespace(last_segment_at=now - 16 * 60, started_at=now - 3600)  # last file before a 15-min outage
    worker = SimpleNamespace(_stream_viewer=viewer, _tasks=[], _stopping=False, _continuous_recorder=rec,
                             _motion_recorder=None, VIEWER_STUCK_SECONDS=CameraWorker.VIEWER_STUCK_SECONDS)
    assert CameraWorker.health(worker) == (True, "ok")  # back 30 s ago: give it time to save a segment
    viewer.connected_at = now - 15 * 60  # pictures for 15 min, still nothing saved: really stalled
    ok, reason = CameraWorker.health(worker)
    assert not ok and "no recording saved" in reason


def test_a_short_camera_drop_is_not_an_outage(monkeypatch):
    import asyncio

    from app.services import status_tracker as st

    flips, events = [], []

    async def set_offline(cid):
        flips.append(cid)

    async def no_db_online(cid):
        st.heartbeat(cid)
        pending = st._pending.pop(cid, None)
        if pending:
            pending.cancel()

    monkeypatch.setattr(st, "OFFLINE_CONFIRM_SECONDS", 0.3)
    monkeypatch.setattr(st, "_set_offline", set_offline)
    monkeypatch.setattr(st, "mark_online", no_db_online)

    async def go():
        # Recorder drops, live view still gets pictures: not offline.
        st.heartbeat(5)
        await st.mark_offline(5)
        for _ in range(4):
            await asyncio.sleep(0.1)
            st.heartbeat(5)
        await asyncio.sleep(0.2)
        assert flips == []
        # Dropped and back within a few seconds: not offline either.
        await st.mark_offline(6)
        await asyncio.sleep(0.1)
        await st.mark_online(6)
        await asyncio.sleep(0.4)
        assert flips == []
        # Really gone: offline once, however many parts report it.
        await st.mark_offline(7)
        await st.mark_offline(7)
        await asyncio.sleep(0.5)
        assert flips == [7]

    asyncio.run(go())


def test_backend_messages_reach_docker_logs_and_a_file_scrubbed(tmp_path, monkeypatch):
    import io
    import logging

    from app.core import log_buffer

    monkeypatch.setattr(log_buffer, "LOG_DIR", str(tmp_path / "logs"))
    out = io.StringIO()
    monkeypatch.setattr(log_buffer.sys, "stdout", out)
    root = logging.getLogger()
    before = list(root.handlers)
    uv = logging.getLogger("uvicorn.error")
    uv_before = list(uv.handlers)
    try:
        log_buffer.install_log_buffer()
        logging.getLogger("app.services.ffmpeg_recorder").warning(
            "Continuous recording failed for camera 3: rtsp://admin:hunter2@192.168.68.203:554/stream1 timed out")
        for h in root.handlers:
            h.flush()
    finally:
        for h in uv.handlers[:]:
            if h not in uv_before:
                uv.removeHandler(h)
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    printed = out.getvalue()
    saved = (tmp_path / "logs" / "lightnvr.log").read_text()
    for text in (printed, saved):
        assert "Continuous recording failed for camera 3" in text and "hunter2" not in text
