from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import require_admin
from app.db.session import AsyncSessionLocal
from app.models.remote_access_settings import RemoteAccessSettings
from app.models.user import User
from app.schemas.remote_access import (
    CloudflareUpdate,
    RemoteAccessSettingsOut,
    RemoteAccessStatusOut,
    TailscaleUpdate,
)
from app.services import cloudflare_manager, tailscale_manager

router = APIRouter(prefix="/api/remote-access", tags=["remote-access"])


def _to_out(record: RemoteAccessSettings) -> RemoteAccessSettingsOut:
    return RemoteAccessSettingsOut(
        tailscale_enabled=record.tailscale_enabled,
        tailscale_hostname=record.tailscale_hostname,
        has_tailscale_authkey=bool(record.tailscale_authkey),
        cloudflare_enabled=record.cloudflare_enabled,
        has_cloudflare_token=bool(record.cloudflare_token),
    )


@router.get("/settings", response_model=RemoteAccessSettingsOut)
async def get_settings(_: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(RemoteAccessSettings, 1)
        return _to_out(record)


@router.put("/tailscale", response_model=RemoteAccessSettingsOut)
async def update_tailscale(payload: TailscaleUpdate, _: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(RemoteAccessSettings, 1)
        if payload.authkey:
            record.tailscale_authkey = payload.authkey
        if payload.hostname:
            record.tailscale_hostname = payload.hostname
        if payload.enabled and not record.tailscale_authkey:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="An auth key is required to enable Tailscale"
            )
        record.tailscale_enabled = payload.enabled
        await db.commit()
        await db.refresh(record)
        authkey, hostname, enabled = record.tailscale_authkey, record.tailscale_hostname, record.tailscale_enabled
        out = _to_out(record)

    await tailscale_manager.set_enabled(enabled, authkey, hostname)
    return out


@router.put("/cloudflare", response_model=RemoteAccessSettingsOut)
async def update_cloudflare(payload: CloudflareUpdate, _: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(RemoteAccessSettings, 1)
        if payload.token:
            record.cloudflare_token = payload.token
        if payload.enabled and not record.cloudflare_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="A tunnel token is required to enable Cloudflare Tunnel"
            )
        record.cloudflare_enabled = payload.enabled
        await db.commit()
        await db.refresh(record)
        token, enabled = record.cloudflare_token, record.cloudflare_enabled
        out = _to_out(record)

    await cloudflare_manager.set_enabled(enabled, token)
    return out


@router.get("/status", response_model=RemoteAccessStatusOut)
async def get_status(_: User = Depends(require_admin)):
    tailscale_status = await tailscale_manager.get_status()
    cloudflare_status = cloudflare_manager.get_status()
    return RemoteAccessStatusOut(tailscale=tailscale_status, cloudflare=cloudflare_status)
