import asyncio
import ipaddress
import logging
from dataclasses import dataclass

from onvif import ONVIFCamera
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.services.onvif_discovery import _WSDL_DIR

logger = logging.getLogger(__name__)

# One camera's push, end to end (connect + 3 SOAP calls). Kept short so a
# whole-fleet push stays interactive even when one camera is unreachable.
PUSH_TIMEOUT_SECONDS = 15


@dataclass
class NtpPushResult:
    camera_id: int
    name: str
    success: bool
    detail: str


def _ntp_manual_entry(server: str) -> dict:
    """ONVIF SetNTP wants to know whether the server is an IP or a hostname."""
    try:
        ipaddress.IPv4Address(server)
        return {"Type": "IPv4", "IPv4Address": server}
    except ValueError:
        return {"Type": "DNS", "DNSname": server}


async def _push_one(host: str, port: int, username: str, password: str, ntp_server: str) -> str:
    camera = ONVIFCamera(host, port, username, password, wsdl_dir=_WSDL_DIR)
    await camera.update_xaddrs()
    device = await camera.create_devicemgmt_service()

    await device.SetNTP({"FromDHCP": False, "NTPManual": [_ntp_manual_entry(ntp_server)]})

    # Switch the camera's clock source to NTP. DaylightSavings is a required
    # element and TimeZone is easy to clobber, so read the camera's current
    # values first and send those back unchanged - this call should only ever
    # change WHERE time comes from, not the camera's local-time settings.
    current = await device.GetSystemDateAndTime()
    request = {
        "DateTimeType": "NTP",
        "DaylightSavings": bool(getattr(current, "DaylightSavings", False)),
    }
    tz = getattr(current, "TimeZone", None)
    if tz is not None and getattr(tz, "TZ", None):
        request["TimeZone"] = {"TZ": tz.TZ}
    await device.SetSystemDateAndTime(request)

    return f"NTP set to {ntp_server}; camera clock now syncs automatically"


async def push_ntp_to_cameras(ntp_server: str) -> list[NtpPushResult]:
    """Push the configured NTP server to every enabled ONVIF camera, using the
    credentials already stored on each camera record. Cameras added via manual
    RTSP (no ONVIF address) are reported as skipped rather than failed - there
    is no ONVIF endpoint to talk to on those.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Camera).where(Camera.enabled == True))  # noqa: E712
        cameras = result.scalars().all()
        snapshot = [
            (c.id, c.name, c.onvif_address, c.username or "", c.password or "")
            for c in cameras
        ]

    results: list[NtpPushResult] = []
    for camera_id, name, onvif_address, username, password in snapshot:
        if not onvif_address:
            results.append(
                NtpPushResult(camera_id, name, False, "Skipped - added manually (no ONVIF address)")
            )
            continue

        host, _, port_str = onvif_address.partition(":")
        port = int(port_str) if port_str.isdigit() else 80

        try:
            detail = await asyncio.wait_for(
                _push_one(host, port, username, password, ntp_server), timeout=PUSH_TIMEOUT_SECONDS
            )
            results.append(NtpPushResult(camera_id, name, True, detail))
        except asyncio.TimeoutError:
            results.append(NtpPushResult(camera_id, name, False, f"Timed out after {PUSH_TIMEOUT_SECONDS}s"))
        except Exception as exc:  # zeep raises library-specific Fault types
            logger.warning("NTP push failed for camera %s (%s): %s", camera_id, name, exc)
            results.append(NtpPushResult(camera_id, name, False, str(exc)[:200]))

    return results
