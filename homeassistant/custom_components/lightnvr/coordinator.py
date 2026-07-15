"""DataUpdateCoordinators for LightNVR.

Two coordinators, matched to how expensive each backend call is:

- LightNVRCameraCoordinator (fast, ~10s default): GET /api/cameras + GET
  /api/cameras/motion-status. Both are cheap, single-round-trip-for-every-
  camera calls, so this drives the camera/binary_sensor/last-motion-sensor
  platforms.
- LightNVRSystemCoordinator (slow, ~60s default): GET /api/system/status +
  GET /api/system/dashboard. The dashboard call aggregates a 7-day heatmap
  query on the backend and none of these values change meaningfully faster
  than a minute, so it's deliberately on its own slower cadence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LightNVRApiClient, LightNVRAuthError, LightNVRConnectionError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class CameraData:
    """Everything the fast coordinator knows about one camera."""

    camera: dict[str, Any]
    motion_active: bool
    motion_last_updated: datetime | None
    last_motion_started_at: datetime | None


@dataclass
class CameraCoordinatorData:
    cameras: dict[int, CameraData] = field(default_factory=dict)


@dataclass
class SystemCoordinatorData:
    status: dict[str, Any] = field(default_factory=dict)
    dashboard: dict[str, Any] = field(default_factory=dict)


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class LightNVRCameraCoordinator(DataUpdateCoordinator[CameraCoordinatorData]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: LightNVRApiClient, update_interval_seconds: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_cameras",
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self._api = api

    async def _async_update_data(self) -> CameraCoordinatorData:
        try:
            cameras = await self._api.async_get_cameras()
            motion = await self._api.async_get_motion_status()
        except LightNVRAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LightNVRConnectionError as err:
            raise UpdateFailed(str(err)) from err

        previous = self.data.cameras if self.data else {}
        result: dict[int, CameraData] = {}

        for cam in cameras:
            camera_id = cam["id"]
            # motion-status is a dict[int, MotionStatusOut] on the backend,
            # but JSON object keys are always strings once deserialized.
            m = motion.get(str(camera_id)) or {}
            is_active = bool(m.get("is_active", False))
            last_updated = _parse_utc(m.get("last_updated"))

            # Edge-detect False->True to get a "last motion STARTED" instant,
            # not just "last time the flag changed" (which the backend's
            # last_updated is - it's touched on both start and stop). Seed
            # from last_updated if we're already seeing it active on the very
            # first observation (e.g. integration reload mid-motion-window).
            prev = previous.get(camera_id)
            was_active = prev.motion_active if prev else False
            if is_active and not was_active:
                started_at = last_updated or datetime.now(timezone.utc)
            elif is_active and was_active:
                started_at = prev.last_motion_started_at if prev else last_updated
            else:
                started_at = prev.last_motion_started_at if prev else None

            result[camera_id] = CameraData(
                camera=cam,
                motion_active=is_active,
                motion_last_updated=last_updated,
                last_motion_started_at=started_at,
            )

        return CameraCoordinatorData(cameras=result)


class LightNVRSystemCoordinator(DataUpdateCoordinator[SystemCoordinatorData]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: LightNVRApiClient, update_interval_seconds: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_system",
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self._api = api

    async def _async_update_data(self) -> SystemCoordinatorData:
        try:
            status = await self._api.async_get_system_status()
            dashboard = await self._api.async_get_dashboard()
        except LightNVRAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LightNVRConnectionError as err:
            raise UpdateFailed(str(err)) from err

        return SystemCoordinatorData(status=status, dashboard=dashboard)
