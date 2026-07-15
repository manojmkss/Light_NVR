"""The LightNVR integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LightNVRApiClient
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_FAST_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SLOW_POLL_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_FAST_POLL_SECONDS,
    DEFAULT_SLOW_POLL_SECONDS,
    DEFAULT_VERIFY_SSL,
)
from .coordinator import LightNVRCameraCoordinator, LightNVRSystemCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CAMERA, Platform.BINARY_SENSOR, Platform.SENSOR]


@dataclass
class LightNVRData:
    api: LightNVRApiClient
    camera_coordinator: LightNVRCameraCoordinator
    system_coordinator: LightNVRSystemCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    verify_ssl = entry.options.get(CONF_VERIFY_SSL, entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL))
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)

    api = LightNVRApiClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
    )

    async def _persist_tokens(access_token: str, refresh_token: str) -> None:
        # Refresh rotates the refresh token on every call - persisting
        # immediately means a later HA restart (before the next natural
        # refresh) reuses a still-valid token instead of an already-spent one.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_ACCESS_TOKEN: access_token, CONF_REFRESH_TOKEN: refresh_token},
        )

    api.on_tokens_updated = _persist_tokens

    fast_seconds = entry.options.get(CONF_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_SECONDS)
    slow_seconds = entry.options.get(CONF_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_SECONDS)

    camera_coordinator = LightNVRCameraCoordinator(hass, entry, api, fast_seconds)
    system_coordinator = LightNVRSystemCoordinator(hass, entry, api, slow_seconds)

    # Raises ConfigEntryAuthFailed / ConfigEntryNotReady as appropriate on
    # failure - HA handles both (starts reauth, or retries setup later).
    await camera_coordinator.async_config_entry_first_refresh()
    await system_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = LightNVRData(
        api=api, camera_coordinator=camera_coordinator, system_coordinator=system_coordinator
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options (poll intervals, verify_ssl) changed - reload to apply them."""
    await hass.config_entries.async_reload(entry.entry_id)
