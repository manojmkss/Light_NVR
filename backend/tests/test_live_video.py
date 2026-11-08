"""Live video through go2rtc (services/live_relay.py): the camera streams are
kept in step with the cameras, only a signed-in user gets a stream and only
one of ours, and the live page's fixed API paths reach the right routes."""

import asyncio
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import live_relay as lr


# ── the relay's streams follow the cameras ──────────────────────────────────


class FakeGo2rtc:
    def __init__(self):
        self.streams: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        self.calls.append((request.method, q))
        if request.method == "GET":
            return httpx.Response(200, json={n: {"producers": [{"url": s}]} for n, s in self.streams.items()})
        if request.method == "PATCH":  # create or update, in memory
            self.streams[q["name"]] = q["src"]
            return httpx.Response(200)
        if request.method == "DELETE":
            # Removed - then go2rtc fails to save its (read-only) config.
            self.streams.pop(q["src"], None)
            return httpx.Response(400, text="yaml: path not exist")
        return httpx.Response(405)


def _relay(monkeypatch, cameras):
    relay = lr.LiveRelay()

    async def desired():
        out = {}
        for cid, main, sub in cameras():
            out[lr.stream_name(cid)] = (sub or main) + lr.SOURCE_OPTIONS
            out[lr.stream_name(cid, "main")] = main + lr.SOURCE_OPTIONS
        return out

    monkeypatch.setattr(relay, "desired", desired)
    return relay


def test_streams_are_registered_updated_and_removed_with_the_cameras(monkeypatch):
    go2rtc = FakeGo2rtc()
    go2rtc.streams["someone_elses"] = "rtsp://x"  # not ours: left alone
    cams = [(1, "rtsp://u:p@cam1/main", "rtsp://u:p@cam1/sub"), (2, "rtsp://u:p@cam2/main", None)]
    relay = _relay(monkeypatch, lambda: cams)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(go2rtc.handler)) as client:
            await relay.sync(client)
            first = dict(go2rtc.streams)
            go2rtc.calls.clear()
            await relay.sync(client)  # nothing changed: nothing re-sent
            unchanged = [c for c in go2rtc.calls if c[0] != "GET"]
            assert not [c for c in go2rtc.calls if c[0] == "PUT"]  # PUT would save passwords to its config
            cams[0] = (1, "rtsp://u:p@cam1/main2", "rtsp://u:p@cam1/sub")
            del cams[1]
            await relay.sync(client)
            return first, unchanged

    first, unchanged = asyncio.run(go())
    assert first["cam1"] == "rtsp://u:p@cam1/sub#media=video#backchannel=0"
    assert first["cam2"] == first["cam2_main"] == "rtsp://u:p@cam2/main#media=video#backchannel=0"
    assert unchanged == []
    assert set(go2rtc.streams) == {"someone_elses", "cam1", "cam1_main"}
    assert go2rtc.streams["cam1_main"].startswith("rtsp://u:p@cam1/main2")
    assert relay.available is True


def test_a_restarted_go2rtc_gets_its_streams_back(monkeypatch):
    go2rtc = FakeGo2rtc()
    relay = _relay(monkeypatch, lambda: [(1, "rtsp://m", "rtsp://s")])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(go2rtc.handler)) as client:
            await relay.sync(client)
            go2rtc.streams.clear()  # restarted
            await relay.sync(client)

    asyncio.run(go())
    assert set(go2rtc.streams) == {"cam1", "cam1_main"}


def test_relay_down_means_pictures(monkeypatch):
    relay = _relay(monkeypatch, lambda: [(1, "rtsp://m", None)])
    relay.available = True

    async def go():
        def down(request):
            raise httpx.ConnectError("no go2rtc")

        async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
            with pytest.raises(httpx.ConnectError):
                await relay.sync(client)

    asyncio.run(go())
    # (the loop turns this into available=False; the sync itself just raises)


def test_stream_names_map_back_to_cameras_and_nothing_else():
    assert lr.camera_of("cam12") == 12 and lr.camera_of("cam3_main") == 3
    for bad in ("cam", "cam1_sub", "rtsp://x", "exec:sh", "cam1#x", "", None):
        assert lr.camera_of(bad) is None


# ── who may open a stream ───────────────────────────────────────────────────


@pytest.fixture
def api(tmp_path):
    import app.models  # noqa: F401
    from app.api.routes import live
    from app.core.security import create_token
    from app.db.session import Base, get_db
    from app.models.camera import Camera
    from app.models.user import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as db:
            db.add(User(id=1, username="viewer", password_hash="x", role="viewer"))
            db.add(Camera(id=1, name="Porch", rtsp_main_url="rtsp://x", enabled=True))
            db.add(Camera(id=2, name="Off", rtsp_main_url="rtsp://y", enabled=False))
            await db.commit()

    asyncio.run(setup())

    async def db():
        async with maker() as s:
            yield s

    app = FastAPI()
    app.include_router(live.router)
    app.dependency_overrides[get_db] = db
    return TestClient(app), create_token("1", "viewer", "access"), create_token("1", "viewer", "refresh")


def _check(client, **query):
    uri = "/api/live/ws?" + urlencode(query)
    return client.get("/api/cameras/live-video/auth", headers={"X-Original-URI": uri}).status_code


def test_only_a_signed_in_user_gets_a_stream_of_an_enabled_camera(api):
    client, token, refresh = api
    assert _check(client, src="cam1", token=token) == 204
    assert _check(client, src="cam1_main", token=token) == 204
    assert _check(client, src="cam1") == 401  # no login
    assert _check(client, src="cam1", token="nonsense") == 401
    assert _check(client, src="cam1", token=refresh) == 401  # a refresh token isn't a login
    assert _check(client, src="cam2", token=token) == 403  # disabled camera
    assert _check(client, src="cam9", token=token) == 403  # no such camera
    assert _check(client, src="rtsp://evil/stream", token=token) == 403  # never an arbitrary source


def test_the_live_pages_fixed_paths_are_not_taken_for_a_camera_id():
    from starlette.routing import Match

    from app.main import app

    def first_route(path):
        scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
        return next(r for r in app.routes if r.matches(scope)[0] == Match.FULL)

    for path in ("/api/cameras/stream-stats", "/api/cameras/motion-status", "/api/cameras/live-video"):
        assert "{camera_id}" not in first_route(path).path, path
    assert first_route("/api/cameras/5").path == "/api/cameras/{camera_id}"


def test_nginx_lets_through_only_the_websocket_of_our_streams():
    import os
    import re

    conf = open(os.path.join(os.path.dirname(__file__), "..", "..", "docker", "nginx", "templates",
                             "default.conf.template"), encoding="utf-8").read()
    block = conf[conf.index("location = /api/live/ws"):]
    block = block[:block.index("\n    }\n")]
    assert "auth_request /_live_auth;" in block
    assert re.search(r'\$arg_src !~ "\^cam\[0-9\]\+\(_main\)\?\$"', block)
    # Only the stream name goes on to go2rtc - not the login token, not any other path.
    assert "proxy_pass http://$go2rtc_upstream/api/ws?src=$arg_src;" in block
    assert conf.count("go2rtc_upstream go2rtc:1984") == 1
    compose = open(os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.yml"), encoding="utf-8").read()
    service = compose[compose.index("  go2rtc:"):compose.index("  nginx:")]
    assert "\n    ports:" not in service  # never published
