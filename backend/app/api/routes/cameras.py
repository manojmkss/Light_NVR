import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.db.session import AsyncSessionLocal, get_db
from app.models.camera import Camera
from app.models.discovery_settings import DiscoverySettings
from app.models.kiosk_view import KioskViewCamera
from app.models.user import User
from app.schemas.camera import (
    CameraCreate,
    CameraOut,
    CameraUpdate,
    DiscoveredDeviceOut,
    DiscoverySettingsOut,
    DiscoverySettingsUpdate,
    ProbeRequest,
    ProbeResponseOut,
    RangeScanRequest,
    TestConnectionRequest,
    TestConnectionResponse,
)
from app.services.onvif_discovery import (
    DEFAULT_SCAN_SUBNETS,
    discover_onvif_devices,
    fetch_camera_profiles,
    find_onvif_port,
    inject_credentials,
    scan_ip_range,
    scan_subnets,
)
from app.services.stream_probe import probe_rtsp_stream

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("/discover", response_model=list[DiscoveredDeviceOut])
async def discover_cameras(_: User = Depends(require_admin)):
    devices = await discover_onvif_devices()
    return [
        DiscoveredDeviceOut(
            host=d.host,
            port=d.port,
            address=d.address,
            scopes=d.scopes,
            hardware_hint=d.hardware_hint,
            name_hint=d.name_hint,
            mac_address=d.mac_address,
        )
        for d in devices
    ]


@router.post("/discover-range", response_model=list[DiscoveredDeviceOut])
async def discover_range(payload: RangeScanRequest, _: User = Depends(require_admin)):
    """Fallback for when multicast discovery finds nothing - scans a
    user-supplied subnet directly with unicast TCP/ONVIF probes, which work
    fine through Docker's NAT even when multicast doesn't reach the LAN at all.
    """
    try:
        devices = await asyncio.wait_for(scan_ip_range(payload.cidr), timeout=150)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Scan took too long - try a smaller range") from exc

    return [
        DiscoveredDeviceOut(
            host=d.host,
            port=d.port,
            address=d.address,
            scopes=d.scopes,
            hardware_hint=d.hardware_hint,
            name_hint=d.name_hint,
            mac_address=d.mac_address,
        )
        for d in devices
    ]


def _parse_subnets(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


@router.get("/discovery-settings", response_model=DiscoverySettingsOut)
async def get_discovery_settings(_: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(DiscoverySettings, 1)
        return DiscoverySettingsOut(custom_subnets=_parse_subnets(record.custom_subnets))


@router.put("/discovery-settings", response_model=DiscoverySettingsOut)
async def update_discovery_settings(payload: DiscoverySettingsUpdate, _: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(DiscoverySettings, 1)
        record.custom_subnets = ",".join(s.strip() for s in payload.custom_subnets if s.strip())
        await db.commit()
        return DiscoverySettingsOut(custom_subnets=_parse_subnets(record.custom_subnets))


@router.post("/discover-default-range", response_model=list[DiscoveredDeviceOut])
async def discover_default_range(_: User = Depends(require_admin)):
    """Automatic fallback once multicast discovery comes back empty - checks
    a curated list of common home-router subnets plus any custom ranges the
    admin has saved, with no manual CIDR entry required for the common case.
    """
    async with AsyncSessionLocal() as db:
        record = await db.get(DiscoverySettings, 1)
        custom = _parse_subnets(record.custom_subnets)

    try:
        # Custom subnets first - the admin added them because their camera is
        # actually there, so it's found in one quick pass instead of waiting
        # through every generic guess first.
        devices = await asyncio.wait_for(scan_subnets(custom + DEFAULT_SCAN_SUBNETS), timeout=320)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Scan took too long") from exc

    return [
        DiscoveredDeviceOut(
            host=d.host,
            port=d.port,
            address=d.address,
            scopes=d.scopes,
            hardware_hint=d.hardware_hint,
            name_hint=d.name_hint,
            mac_address=d.mac_address,
        )
        for d in devices
    ]


@router.post("/probe", response_model=ProbeResponseOut)
async def probe_camera(payload: ProbeRequest, _: User = Depends(require_admin)):
    # Resolve the port: if the caller didn't specify one (or gave None), scan
    # common ONVIF ports to find whichever one the camera actually answers on.
    # This means the user never has to know their camera's ONVIF port in advance.
    try:
        port = await find_onvif_port(payload.host, preferred_port=payload.port)
    except ConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        info = await fetch_camera_profiles(payload.host, port, payload.username, payload.password)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Could not connect to ONVIF device: {exc}") from exc

    # If RTSP validation succeeded, profile URIs already carry the working
    # credentials and scheme (set inside _fetch_camera_profiles). For cameras
    # where RTSP validation failed (no network path to RTSP), fall back to
    # injecting the given credentials so the form still shows something useful.
    effective_user = info.resolved_username or payload.username
    profiles_out = []
    for p in info.profiles:
        # URI is already credential-injected when validated_main_url is set
        uri = p.stream_uri if info.validated_main_url else inject_credentials(
            p.stream_uri, effective_user, payload.password
        )
        profiles_out.append({
            "token": p.token,
            "name": p.name,
            "stream_uri": uri,
            "width": p.width,
            "height": p.height,
        })

    return ProbeResponseOut(
        manufacturer=info.manufacturer,
        model=info.model,
        firmware_version=info.firmware_version,
        recommended_main_token=info.recommended_main_token,
        recommended_sub_token=info.recommended_sub_token,
        detected_port=port,
        profiles=profiles_out,
        validated_main_url=info.validated_main_url,
        validated_sub_url=info.validated_sub_url,
        resolved_username=info.resolved_username,
        codec=info.codec,
        has_audio=info.has_audio,
    )


@router.post("/test-connection", response_model=TestConnectionResponse)
async def test_connection(payload: TestConnectionRequest, _: User = Depends(require_admin)):
    try:
        info = await probe_rtsp_stream(payload.rtsp_url)
    except ConnectionError as exc:
        return TestConnectionResponse(success=False, error=str(exc))

    return TestConnectionResponse(
        success=True,
        codec=info.codec,
        has_audio=info.has_audio,
        width=info.width,
        height=info.height,
        fps=info.fps,
    )


@router.get("", response_model=list[CameraOut])
async def list_cameras(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    result = await db.execute(select(Camera))
    return result.scalars().all()


@router.get("/{camera_id}", response_model=CameraOut)
async def get_camera(camera_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")
    return camera


@router.post("", response_model=CameraOut, status_code=status.HTTP_201_CREATED)
async def create_camera(payload: CameraCreate, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    camera = Camera(**payload.model_dump())
    db.add(camera)
    await db.commit()
    await db.refresh(camera)

    from app.services.camera_supervisor import supervisor

    await supervisor.sync_camera(camera)
    return camera


@router.put("/{camera_id}", response_model=CameraOut)
async def update_camera(
    camera_id: int,
    payload: CameraUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(camera, field, value)

    await db.commit()
    await db.refresh(camera)

    from app.services.camera_supervisor import supervisor

    await supervisor.sync_camera(camera)
    return camera


@router.delete("/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(camera_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Camera not found")

    from app.services.camera_supervisor import supervisor

    await supervisor.remove_camera(camera_id)
    # SQLite's FK cascade isn't active on this connection, so the
    # ondelete="CASCADE" on KioskViewCamera.camera_id is enforced here
    # explicitly rather than relying on it - otherwise a deleted camera
    # leaves a dangling reference in any kiosk view that included it.
    await db.execute(KioskViewCamera.__table__.delete().where(KioskViewCamera.camera_id == camera_id))
    await db.delete(camera)
    await db.commit()
