"""Camera platform for LightNVR.

Live view is proxied through LightNVR's own authenticated REST API - this
entity deliberately does NOT implement stream_source() and does NOT set
CameraEntityFeature.STREAM, which is what keeps Home Assistant off the
RTSP/HLS/go2rtc pipeline entirely. Every byte this entity ever returns
(stills via async_camera_image, live view via handle_async_mjpeg_stream)
comes from LightNVR's HTTP API, never a direct connection to the camera.
"""

from __future__ import annotations

import logging

import aiohttp
from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import LightNVRApiClient, LightNVRError
from .coordinator import LightNVRCameraCoordinator
from .entity import LightNVRCameraEntity

_LOGGER = logging.getLogger(__name__)

# Chunk size for piping the upstream MJPEG multipart stream through to the HA
# frontend - large enough to avoid excessive syscalls, small enough to keep
# live view responsive rather than buffering whole frames before forwarding.
_PROXY_CHUNK_SIZE = 16 * 1024


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = entry.runtime_data
    coordinator = data.camera_coordinator
    known_ids: set[int] = set()

    def _add_new_cameras() -> None:
        if not coordinator.data:
            return
        new_ids = set(coordinator.data.cameras) - known_ids
        if not new_ids:
            return
        known_ids.update(new_ids)
        entities: list[LightNVRCamera] = []
        for camera_id in sorted(new_ids):
            entities.append(LightNVRCamera(coordinator, data.api, entry.entry_id, camera_id, "sub"))
            entities.append(LightNVRCamera(coordinator, data.api, entry.entry_id, camera_id, "main"))
        async_add_entities(entities)

    _add_new_cameras()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))


class LightNVRCamera(LightNVRCameraEntity, Camera):
    def __init__(
        self,
        coordinator: LightNVRCameraCoordinator,
        api: LightNVRApiClient,
        entry_id: str,
        camera_id: int,
        quality: str,
    ) -> None:
        # Two unrelated base classes, each with its own __init__ - called
        # explicitly rather than relying on a super() chain to reach both,
        # since CoordinatorEntity and Camera aren't designed with each other
        # in mind. This is the common pattern other integrations combining
        # CoordinatorEntity with a platform-specific entity base use.
        LightNVRCameraEntity.__init__(self, coordinator, entry_id, camera_id)
        Camera.__init__(self)
        self._api = api
        self._quality = quality
        is_hd = quality == "main"
        self._attr_translation_key = "camera_hd" if is_hd else "camera"
        self._attr_unique_id = self.unique_id_suffix("camera_hd" if is_hd else "camera")
        # The HD/main-stream tile is opt-in: it spins up an on-demand decode
        # on the LightNVR backend (torn down ~30s after the last viewer
        # disconnects), so it shouldn't be enabled for everyone by default
        # the way the always-on substream tile is.
        self._attr_entity_registry_enabled_default = not is_hd

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            return await self._api.async_get_snapshot(self.camera_id)
        except LightNVRError as err:
            _LOGGER.debug("Snapshot fetch failed for LightNVR camera %s: %s", self.camera_id, err)
            return None

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        try:
            headers = await self._api.async_auth_header()
        except LightNVRError as err:
            _LOGGER.warning("Could not authenticate live stream for LightNVR camera %s: %s", self.camera_id, err)
            return None

        url = self._api.mjpeg_url(self.camera_id, self._quality)
        try:
            upstream = await self._api.session.get(url, headers=headers)
        except aiohttp.ClientError as err:
            _LOGGER.warning("Could not open live stream for LightNVR camera %s: %s", self.camera_id, err)
            return None

        response: web.StreamResponse | None = None
        try:
            response = web.StreamResponse(
                status=upstream.status,
                headers={"Content-Type": upstream.headers.get("Content-Type", "multipart/x-mixed-replace")},
            )
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(_PROXY_CHUNK_SIZE):
                await response.write(chunk)
        except (ConnectionResetError, ConnectionError, aiohttp.ClientError):
            # Normal, not an error worth logging: the viewer navigated away/
            # HA is tearing the request down, or the backend stream ended.
            pass
        finally:
            upstream.close()
        return response
