"""Self-healing for cameras that change IP (the classic DHCP-lease problem).

When a camera's host stops responding, this finds the same physical device at
its new address by matching the ONVIF serial number, rewrites the camera's
stream URLs / ONVIF address, and brings it back online - no manual re-add.

Only cameras added via ONVIF have a stored serial (`hardware_id`), so this is
strictly a bonus recovery path: manual-RTSP cameras and cameras whose device
doesn't report a serial simply fall back to the existing offline behaviour.
"""

import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.models.discovery_settings import DiscoverySettings
from app.services.events import emit_event
from app.services.onvif_discovery import (
    DEFAULT_SCAN_SUBNETS,
    CameraProfileInfo,
    _tcp_probe,
    fetch_camera_profiles,
    find_onvif_port,
    get_device_serial,
    scan_ip_range,
)

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 180      # how often the loop looks for stuck cameras
OFFLINE_GRACE_SECONDS = 300       # ignore blips - only relocate after 5 min down
RELOCATE_COOLDOWN_SECONDS = 900   # don't re-attempt the same camera for 15 min
_SERIAL_PROBE_TIMEOUT = 6.0

# Per-camera monotonic time of the last attempt. In-memory: a failed attempt
# shouldn't hammer the network, and losing this on restart is harmless.
_last_attempt: dict[int, float] = {}

# Subnet scans are heavy; run at most one relocation at a time.
_lock = asyncio.Lock()


def apply_profile_info(camera: Camera, info: CameraProfileInfo, host: str, port: int) -> None:
    """Write a fresh probe's validated results onto a camera record. Shared by
    redetect (same address) and relocation (new address)."""
    if info.validated_main_url:
        camera.rtsp_main_url = info.validated_main_url
    if info.validated_sub_url:
        camera.rtsp_sub_url = info.validated_sub_url
    if info.resolved_username:
        camera.username = info.resolved_username
    if info.codec in ("h264", "h265"):
        camera.codec = info.codec
    camera.has_audio = info.has_audio
    if info.serial_number:
        camera.hardware_id = info.serial_number
    camera.onvif_address = f"{host}:{port}"


def _split_addr(onvif_address: str) -> tuple[str, int]:
    host, _, port_str = onvif_address.partition(":")
    return host, (int(port_str) if port_str.isdigit() else 80)


def _subnet_of(ip: str) -> str | None:
    try:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False))
    except ValueError:
        return None


async def _custom_subnets() -> list[str]:
    async with AsyncSessionLocal() as db:
        record = await db.get(DiscoverySettings, 1)
    if not record or not record.custom_subnets:
        return []
    return [s.strip() for s in record.custom_subnets.split(",") if s.strip()]


async def relocate_camera(camera_id: int, *, force: bool = False) -> tuple[bool, str]:
    """Find `camera_id` at a new IP by serial and update it in place.

    Returns (relocated, message). `force` skips the cooldown (the manual
    "Locate on network" button); the automatic loop respects it.
    """
    async with _lock:
        async with AsyncSessionLocal() as db:
            camera = await db.get(Camera, camera_id)
            if camera is None:
                return False, "Camera not found"
            name = camera.name
            hardware_id = (camera.hardware_id or "").strip()
            username = camera.username or ""
            password = camera.password or ""
            onvif_address = camera.onvif_address or ""

        if not hardware_id:
            return False, "No stored device identity - add or Re-detect this camera via ONVIF first."
        if not onvif_address:
            return False, "This camera has no ONVIF address to relocate from."

        _last_attempt[camera_id] = time.monotonic()
        old_host, old_port = _split_addr(onvif_address)

        # If the old address still answers on its ONVIF port, this isn't an
        # IP-change problem (more likely wrong credentials or a camera-side
        # fault) - a subnet scan wouldn't help, so don't spend one.
        if await _tcp_probe(old_host, old_port):
            return False, f"{old_host} still responds - not an IP change (check credentials / the camera itself)."

        # Search the old /24 first (DHCP almost always re-leases within the same
        # subnet), then any other configured subnets.
        subnets: list[str] = []
        old_subnet = _subnet_of(old_host)
        if old_subnet:
            subnets.append(old_subnet)
        for cidr in (await _custom_subnets()) + DEFAULT_SCAN_SUBNETS:
            if cidr not in subnets:
                subnets.append(cidr)

        for cidr in subnets:
            try:
                devices = await scan_ip_range(cidr)
            except ValueError:
                continue
            for dev in devices:
                if dev.host == old_host:
                    continue  # the address we already know is dead
                serial = await get_device_serial(
                    dev.host, dev.port, username, password, timeout=_SERIAL_PROBE_TIMEOUT
                )
                if not serial or serial.strip() != hardware_id:
                    continue

                # Found it. Run the full validated probe at the new address and
                # write it back.
                try:
                    port = await find_onvif_port(dev.host, preferred_port=dev.port)
                    info = await fetch_camera_profiles(dev.host, port, username, password)
                except Exception as exc:  # noqa: BLE001 - report, don't crash the loop
                    logger.warning("Relocator: matched camera %s at %s but re-probe failed: %s",
                                   camera_id, dev.host, exc)
                    return False, f"Found the camera at {dev.host} but couldn't read its streams: {exc}"

                if not info.validated_main_url:
                    return False, f"Found the camera at {dev.host} but no stream validated."

                async with AsyncSessionLocal() as db:
                    camera = await db.get(Camera, camera_id)
                    if camera is None:
                        return False, "Camera was deleted during relocation."
                    apply_profile_info(camera, info, dev.host, port)
                    await db.commit()
                    await db.refresh(camera)
                    fresh = camera

                from app.services.camera_supervisor import supervisor
                await supervisor.sync_camera(fresh)

                await emit_event(
                    camera_id,
                    "system",
                    f"Camera '{name}' moved from {old_host} to {dev.host} (likely a DHCP change) - "
                    f"address updated automatically and reconnecting.",
                )
                logger.info("Relocator: camera %s moved %s -> %s", camera_id, old_host, dev.host)
                return True, f"Relocated to {dev.host}."

        return False, f"Couldn't find this camera on the network (searched {len(subnets)} subnet(s))."


async def _find_relocation_candidates() -> list[int]:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Camera).where(Camera.enabled.is_(True), Camera.status == "offline")
        )
        cameras = result.scalars().all()

    candidates: list[int] = []
    for c in cameras:
        if not (c.hardware_id or "").strip() or not c.onvif_address:
            continue
        # Been offline long enough to not be a transient blip?
        if c.last_seen_at is not None:
            offline_for = (now - c.last_seen_at.replace(tzinfo=timezone.utc)).total_seconds()
            if offline_for < OFFLINE_GRACE_SECONDS:
                continue
        if time.monotonic() - _last_attempt.get(c.id, 0.0) < RELOCATE_COOLDOWN_SECONDS:
            continue
        candidates.append(c.id)
    return candidates


async def camera_relocator_loop() -> None:
    """Background loop: periodically try to recover cameras stuck offline by
    finding them at a new IP. Cheap when nothing is wrong - it only scans the
    network when a camera has actually been unreachable for a while."""
    while True:
        try:
            for camera_id in await _find_relocation_candidates():
                relocated, message = await relocate_camera(camera_id)
                if relocated:
                    logger.info("Relocator: camera %s recovered - %s", camera_id, message)
                else:
                    logger.debug("Relocator: camera %s not relocated - %s", camera_id, message)
        except Exception:
            logger.exception("Camera relocator loop iteration failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
