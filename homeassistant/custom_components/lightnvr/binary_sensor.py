"""Binary sensors for LightNVR: per-camera motion and connectivity."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import LightNVRCameraCoordinator
from .entity import LightNVRCameraEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.camera_coordinator
    known_ids: set[int] = set()

    def _add_new() -> None:
        if not coordinator.data:
            return
        new_ids = set(coordinator.data.cameras) - known_ids
        if not new_ids:
            return
        known_ids.update(new_ids)
        entities = []
        for camera_id in sorted(new_ids):
            entities.append(LightNVRMotionSensor(coordinator, entry.entry_id, camera_id))
            entities.append(LightNVRConnectivitySensor(coordinator, entry.entry_id, camera_id))
        async_add_entities(entities)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class LightNVRMotionSensor(LightNVRCameraEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_translation_key = "motion"

    def __init__(self, coordinator: LightNVRCameraCoordinator, entry_id: str, camera_id: int) -> None:
        super().__init__(coordinator, entry_id, camera_id)
        self._attr_unique_id = self.unique_id_suffix("motion")

    @property
    def is_on(self) -> bool | None:
        data = self.camera_data
        return data.motion_active if data else None


class LightNVRConnectivitySensor(LightNVRCameraEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connectivity"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LightNVRCameraCoordinator, entry_id: str, camera_id: int) -> None:
        super().__init__(coordinator, entry_id, camera_id)
        self._attr_unique_id = self.unique_id_suffix("connectivity")

    @property
    def is_on(self) -> bool | None:
        # Inherits LightNVRCameraEntity.available as-is: it only checks the
        # coordinator succeeded and the camera still exists in LightNVR,
        # regardless of this status field - a camera reporting "offline" is
        # exactly the normal, available state this entity exists to report.
        data = self.camera_data
        return data.camera.get("status") == "online" if data else None
