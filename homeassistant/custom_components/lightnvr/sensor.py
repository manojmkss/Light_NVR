"""Sensors for LightNVR: last-motion time per camera, plus system-wide
CPU/memory/storage/uptime and today's-activity counters on the hub device.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import LightNVRCameraCoordinator, LightNVRSystemCoordinator, SystemCoordinatorData
from .entity import LightNVRCameraEntity, LightNVRHubEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = entry.runtime_data

    async_add_entities(
        LightNVRSystemSensor(data.system_coordinator, entry.entry_id, entry.title, description)
        for description in SYSTEM_SENSOR_DESCRIPTIONS
    )

    camera_coordinator = data.camera_coordinator
    known_ids: set[int] = set()

    def _add_new() -> None:
        if not camera_coordinator.data:
            return
        new_ids = set(camera_coordinator.data.cameras) - known_ids
        if not new_ids:
            return
        known_ids.update(new_ids)
        async_add_entities(
            LightNVRLastMotionSensor(camera_coordinator, entry.entry_id, camera_id) for camera_id in sorted(new_ids)
        )

    _add_new()
    entry.async_on_unload(camera_coordinator.async_add_listener(_add_new))


class LightNVRLastMotionSensor(LightNVRCameraEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_motion"

    def __init__(self, coordinator: LightNVRCameraCoordinator, entry_id: str, camera_id: int) -> None:
        super().__init__(coordinator, entry_id, camera_id)
        self._attr_unique_id = self.unique_id_suffix("last_motion")

    @property
    def native_value(self) -> datetime | None:
        data = self.camera_data
        return data.last_motion_started_at if data else None


@dataclass(frozen=True, kw_only=True)
class LightNVRSystemSensorDescription(SensorEntityDescription):
    value_fn: Callable[[SystemCoordinatorData], Any] = lambda data: None
    attrs_fn: Callable[[SystemCoordinatorData], dict[str, Any]] | None = None


def _uptime_to_boot_timestamp(data: SystemCoordinatorData) -> datetime | None:
    # Exposed as a boot-time timestamp rather than a raw duration - a
    # duration would re-fire every poll and pollute recorder history for no
    # benefit; a timestamp only changes state on an actual restart, which is
    # the current Home Assistant convention for "uptime"-style sensors.
    uptime = data.status.get("uptime_seconds")
    if uptime is None:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=uptime)


SYSTEM_SENSOR_DESCRIPTIONS: tuple[LightNVRSystemSensorDescription, ...] = (
    LightNVRSystemSensorDescription(
        key="cpu_percent",
        translation_key="cpu",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.get("cpu_percent"),
    ),
    LightNVRSystemSensorDescription(
        key="memory_percent",
        translation_key="memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.get("memory_percent"),
        attrs_fn=lambda data: {
            "used_bytes": data.status.get("memory_used_bytes"),
            "total_bytes": data.status.get("memory_total_bytes"),
        },
    ),
    LightNVRSystemSensorDescription(
        key="storage_used",
        translation_key="storage_used",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.get("storage_used_bytes"),
    ),
    LightNVRSystemSensorDescription(
        key="storage_free",
        translation_key="storage_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.get("storage_free_bytes"),
    ),
    LightNVRSystemSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_uptime_to_boot_timestamp,
    ),
    LightNVRSystemSensorDescription(
        key="cameras_offline",
        translation_key="cameras_offline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.get("cameras_offline"),
    ),
    # "Today" counters reset to 0 at local midnight on the backend -
    # TOTAL_INCREASING is the state class HA statistics expects for exactly
    # that shape (a monotonically-increasing counter that periodically
    # resets), matching e.g. how daily-energy sensors are classified.
    LightNVRSystemSensorDescription(
        key="motion_events_today",
        translation_key="motion_events_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.dashboard.get("motion_events_today"),
    ),
    LightNVRSystemSensorDescription(
        key="recordings_today",
        translation_key="recordings_today",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.dashboard.get("recordings_today"),
    ),
    LightNVRSystemSensorDescription(
        key="recording_failures_today",
        translation_key="recording_failures_today",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda data: data.dashboard.get("recording_failures_today"),
    ),
    LightNVRSystemSensorDescription(
        key="storage_days_to_full",
        translation_key="storage_days_to_full",
        native_unit_of_measurement=UnitOfTime.DAYS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.dashboard.get("storage_days_to_full"),
    ),
)


class LightNVRSystemSensor(LightNVRHubEntity, SensorEntity):
    entity_description: LightNVRSystemSensorDescription

    def __init__(
        self,
        coordinator: LightNVRSystemCoordinator,
        entry_id: str,
        hub_name: str,
        description: LightNVRSystemSensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id, hub_name)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        if not self.coordinator.data:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.coordinator.data or not self.entity_description.attrs_fn:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
