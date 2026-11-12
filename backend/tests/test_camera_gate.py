"""The per-camera gate (services/camera_gate.py): a camera that refuses or
can't be reached gets a rest from all of the NVR's connections at once, and
a failing optional stream never delays recording."""

import asyncio
import threading

import pytest

from app.services import camera_gate as cg


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def gate():
    clock = Clock()
    return cg.CameraGate(clock=clock), clock


def test_failures_rest_optional_connections_but_never_delay_recording(gate):
    g, clock = gate
    g.failed(3, "could not open")  # e.g. face capture refused an extra stream
    assert g.wait_seconds(3, essential=True) == 0  # recording carries on
    assert g.wait_seconds(3, essential=False) == pytest.approx(10)
    g.failed(3, "could not open")
    assert g.wait_seconds(3, essential=False) == pytest.approx(20)  # doubling
    for _ in range(10):
        g.failed(3, "Connection timed out")
    assert g.wait_seconds(3, essential=False) == pytest.approx(300)  # capped
    assert g.wait_seconds(1, essential=False) == 0  # other cameras unaffected


def test_a_camera_answering_403_gets_a_rest_from_everything(gate):
    g, clock = gate
    g.failed(3, "method SETUP failed: 403 Forbidden")
    assert g.wait_seconds(3, essential=True) == pytest.approx(15)
    assert g.wait_seconds(3, essential=False) == pytest.approx(15)
    g.failed(3, "Server returned 403 Forbidden (access denied)")
    assert g.wait_seconds(3, essential=True) == pytest.approx(30)


def test_after_a_rest_one_connection_tries_first(gate):
    g, clock = gate
    g.failed(3, "403 Forbidden")
    clock.t += 31  # rests over (15 s refused, 10 s optional)
    assert g.wait_seconds(3, essential=True) == 0  # the recorder tries
    assert g.wait_seconds(3, essential=True) > 0  # the motion decoder waits for its answer
    assert g.wait_seconds(3, essential=False) > 0  # so do face capture and live view
    g.ok(3)  # it worked: everyone goes
    assert g.wait_seconds(3, essential=True) == 0
    assert g.wait_seconds(3, essential=False) == 0


def test_recording_never_waits_for_an_optional_connection_that_is_trying(gate):
    g, clock = gate
    g.failed(3, "403 Forbidden")
    clock.t += 31
    assert g.wait_seconds(3, essential=False) == 0  # face capture got there first
    assert g.wait_seconds(3, essential=True) == 0  # recording still tries at once


def test_a_trier_that_never_answers_frees_the_turn(gate):
    g, clock = gate
    g.failed(3, "403 Forbidden")
    clock.t += 31
    assert g.wait_seconds(3, essential=True) == 0
    clock.t += cg.PROBE_SECONDS + 1
    assert g.wait_seconds(3, essential=True) == 0


def test_waiting_returns_early_when_stopped():
    g = cg.CameraGate()
    g.failed(3, "403 Forbidden")
    stop = threading.Event()
    stop.set()
    assert g.wait_thread(3, essential=True, stop=stop) is False

    async def go():
        ev = asyncio.Event()
        ev.set()
        return await g.wait(3, essential=True, stop=ev)

    assert asyncio.run(go()) is False


@pytest.mark.parametrize("detail,refused,connection", [
    ("method SETUP failed: 403 Forbidden", True, True),
    ("Server returned 403 Forbidden (access denied)", True, True),
    ("Failed reading RTSP data: Connection timed out", False, True),
    ("Connection to tcp://10.0.0.5:554 failed: Connection refused", False, True),
    ("Could not find tag for codec pcm_mulaw in stream #1", False, False),  # about the sound, not the camera
])
def test_what_counts_as_refused_or_unreachable(detail, refused, connection):
    assert cg.looks_refused(detail) is refused
    assert cg.looks_like_connection_problem(detail) is connection


def test_an_unreachable_camera_doesnt_get_its_sound_tested_on_every_retry(tmp_path, monkeypatch):
    """The sound test opens extra connections; when the camera can't be reached
    it's tried again only after AUDIO_RETRY_SECONDS, not on every attempt."""
    from app.services import ffmpeg_recorder

    calls = []

    async def resolve(camera_id, url, has_audio):
        calls.append(1)
        return None  # undecided: the camera couldn't be reached

    monkeypatch.setattr(ffmpeg_recorder, "_resolve_audio_mode", resolve)
    rec = ffmpeg_recorder.ContinuousRecorder(1, "rtsp://127.0.0.1:1/x", True)

    async def go():
        modes = [await rec._resolve_audio_mode() for _ in range(5)]
        return modes

    assert asyncio.run(go()) == ["aac"] * 5  # the old default meanwhile
    assert len(calls) == 1
    rec._audio_retry_at = 0  # ten minutes later
    asyncio.run(rec._resolve_audio_mode())
    assert len(calls) == 2
