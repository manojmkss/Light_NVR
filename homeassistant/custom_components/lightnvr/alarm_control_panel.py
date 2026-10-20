"""LightNVR's arm mode (Home / Away / Night / Disarmed) as an alarm panel.

Changing it here changes who LightNVR alerts about what (through its alert
rules); it never changes recording."""

from __future__ import annotations

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
)

try:  # Home Assistant 2024.11+
    from homeassistant.components.alarm_control_panel import AlarmControlPanelState

    _NEW_API = True
    _STATES = {
        "home": AlarmControlPanelState.ARMED_HOME,
        "away": AlarmControlPanelState.ARMED_AWAY,
        "night": AlarmControlPanelState.ARMED_NIGHT,
        "disarmed": AlarmControlPanelState.DISARMED,
    }
except ImportError:  # older releases
    from homeassistant.const import (
        STATE_ALARM_ARMED_AWAY,
        STATE_ALARM_ARMED_HOME,
        STATE_ALARM_ARMED_NIGHT,
        STATE_ALARM_DISARMED,
    )

    _NEW_API = False
    _STATES = {
        "home": STATE_ALARM_ARMED_HOME,
        "away": STATE_ALARM_ARMED_AWAY,
        "night": STATE_ALARM_ARMED_NIGHT,
        "disarmed": STATE_ALARM_DISARMED,
    }

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import LightNVRHubEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = entry.runtime_data
    if not data.system_coordinator.data or not data.system_coordinator.data.security:
        return  # LightNVR older than 0.4 - no modes to control
    async_add_entities([LightNVRModePanel(data.system_coordinator, data.api, entry.entry_id, entry.title)])


class LightNVRModePanel(LightNVRHubEntity, AlarmControlPanelEntity):
    _attr_name = "Security mode"
    _attr_code_arm_required = False
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )

    def __init__(self, coordinator, api, entry_id: str, hub_name: str) -> None:
        super().__init__(coordinator, entry_id, hub_name)
        self._api = api
        self._attr_unique_id = f"{entry_id}_security_mode"

    @property
    def _mode(self) -> str | None:
        sec = self.coordinator.data.security if self.coordinator.data else {}
        return sec.get("mode")

    @property
    def alarm_state(self):
        return _STATES.get(self._mode or "")

    @property
    def extra_state_attributes(self):
        sec = self.coordinator.data.security if self.coordinator.data else {}
        return {"source": sec.get("source"), "next_change_at": sec.get("next_change_at")}

    async def _set(self, mode: str) -> None:
        await self._api.async_set_security_mode(mode)
        await self.coordinator.async_request_refresh()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._set("disarmed")

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._set("home")

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._set("away")

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._set("night")


if not _NEW_API:
    # Before 2024.11 Home Assistant read `state` directly (overriding it on
    # newer releases is rejected, hence only here).
    LightNVRModePanel.state = property(lambda self: self.alarm_state)
