"""Live video in the browser: the cameras' own H.264/H.265, relayed by go2rtc.

The live grid used to be MJPEG - one JPEG per frame, 5-10 Mbit/s per tile
and 17-33 Mbit/s for a full-screen main stream. That's fine on the home
Wi-Fi and hopeless over mobile data through Tailscale. go2rtc (a sidecar,
docker-compose.yml) instead passes the camera's own compressed video to the
browser as fragmented MP4 over a websocket (MSE): about 1 Mbit/s per tile.

go2rtc publishes no ports. The browser reaches it only through nginx, which
lets through nothing but its MSE websocket, for a stream name of ours, after
the backend has checked the user's login (`ws_auth` below). go2rtc's own API
- which can list the cameras' passwords and add arbitrary sources - is only
reachable on the internal Docker network, where this module keeps its
streams in step with the cameras:

- cam<id>       the camera's sub stream (the grid)
- cam<id>_main  its main stream (full screen)

Streams are registered at runtime (nothing with a password is written to
disk) and re-registered whenever go2rtc restarts. go2rtc only connects to a
camera while someone is watching it.
"""

import asyncio
import logging
import os
import re

import httpx
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera

logger = logging.getLogger(__name__)

GO2RTC_URL = os.environ.get("LIGHTNVR_GO2RTC_URL", "http://go2rtc:1984").rstrip("/")
SYNC_SECONDS = 30
STREAM_NAME = re.compile(r"^cam(\d+)(_main)?$")
# Video only (the live grid is muted anyway - less data), and no ONVIF
# two-way audio channel: some cameras refuse the whole session when asked
# for one.
SOURCE_OPTIONS = "#media=video#backchannel=0"


def stream_name(camera_id: int, quality: str = "sub") -> str:
    return f"cam{camera_id}_main" if quality == "main" else f"cam{camera_id}"


def camera_of(name: str) -> int | None:
    m = STREAM_NAME.match(name or "")
    return int(m.group(1)) if m else None


class LiveRelay:
    def __init__(self) -> None:
        self.available = False
        self._registered: dict[str, str] = {}
        self._kick = asyncio.Event()

    def kick(self) -> None:
        """Cameras changed: sync now rather than at the next tick."""
        self._kick.set()

    async def desired(self) -> dict[str, str]:
        async with AsyncSessionLocal() as db:
            cams = (await db.execute(select(Camera).where(Camera.enabled.is_(True)))).scalars().all()
        out = {}
        for c in cams:
            if not c.rtsp_main_url:
                continue
            out[stream_name(c.id)] = (c.rtsp_sub_url or c.rtsp_main_url) + SOURCE_OPTIONS
            out[stream_name(c.id, "main")] = c.rtsp_main_url + SOURCE_OPTIONS
        return out

    async def sync(self, client: httpx.AsyncClient) -> None:
        want = await self.desired()
        r = await client.get(f"{GO2RTC_URL}/api/streams")
        r.raise_for_status()
        have = set((r.json() or {}).keys())
        if not have & set(self._registered):
            self._registered = {}  # go2rtc restarted: it has forgotten everything
        # PATCH creates or updates a stream in go2rtc's memory only. (PUT and
        # DELETE also try to save its config file - mounted read-only on
        # purpose, so camera passwords never land on disk - and then answer
        # 400 although the change was made.)
        for name in sorted(have):
            if camera_of(name) is not None and name not in want:
                await client.delete(f"{GO2RTC_URL}/api/streams", params={"src": name})
                self._registered.pop(name, None)
        for name, src in want.items():
            if name in have and self._registered.get(name) == src:
                continue
            r = await client.patch(f"{GO2RTC_URL}/api/streams", params={"name": name, "src": src})
            r.raise_for_status()
            self._registered[name] = src
        if not self.available:
            logger.info("Live video relay ready (%d streams)", len(want))
        self.available = True

    async def loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    await self.sync(client)
                except Exception as exc:
                    if self.available:
                        logger.warning("Live video relay unavailable (%s) - live view falls back to pictures", exc)
                    self.available = False
                    self._registered = {}
                try:
                    await asyncio.wait_for(self._kick.wait(), timeout=SYNC_SECONDS)
                except asyncio.TimeoutError:
                    pass
                self._kick.clear()


live_relay = LiveRelay()
