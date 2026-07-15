"""Shared entity base classes: device grouping and availability.

Two device tiers:
- One hub device per config entry, holding the system-wide sensors.
- One device per camera (via_device = hub), holding that camera's
  camera/binary_sensor/sensor entities.

Unique IDs are scoped by entry_id (not just camera_id) throughout, since
LightNVR's camera IDs are plain SQLite auto-increment integers - two
LightNVR instances added as two config entries could otherwise collide on
camera_id=1.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import CameraCoordinatorData, LightNVRCameraCoordinator, LightNVRSystemCoordinator, SystemCoordinatorData


class LightNVRHubEntity(CoordinatorEntity[SystemCoordinatorData]):
    """Base for entities on the hub device (system-wide sensors)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LightNVRSystemCoordinator, entry_id: str, hub_name: str) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=hub_name,
            manufacturer=MANUFACTURER,
        )


class LightNVRCameraEntity(CoordinatorEntity[CameraCoordinatorData]):
    """Base for entities on a per-camera device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LightNVRCameraCoordinator, entry_id: str, camera_id: int) -> None:
        super().__init__(coordinator)
        self._entry_id = entry_id
        self.camera_id = camera_id
        cam = coordinator.data.cameras.get(camera_id) if coordinator.data else None
        name = cam.camera["name"] if cam else f"Camera {camera_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{camera_id}")},
            name=name,
            manufacturer=MANUFACTURER,
            via_device=(DOMAIN, entry_id),
        )

    @property
    def camera_data(self):
        """The coordinator's current data for this camera, or None if the
        camera has since disappeared from LightNVR (deleted, or a transient
        list-fetch miss) - entities use this to drive `available`.
        """
        if not self.coordinator.data:
            return None
        return self.coordinator.data.cameras.get(self.camera_id)

    @property
    def available(self) -> bool:
        # CoordinatorEntity.available already checks last_update_success;
        # this adds "and the camera itself still exists" on top of that.
        return super().available and self.camera_data is not None

    def unique_id_suffix(self, suffix: str) -> str:
        return f"{self._entry_id}_{self.camera_id}_{suffix}"
