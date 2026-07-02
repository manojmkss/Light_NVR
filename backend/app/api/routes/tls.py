from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.core.deps import require_admin
from app.core.uploads import read_limited
from app.db.session import AsyncSessionLocal
from app.models.tls_settings import TlsSettings
from app.models.user import User
from app.schemas.tls import LetsEncryptRequest, TlsActionResult, TlsSettingsOut
from app.services.tls_manager import generate_self_signed, request_letsencrypt_cert, save_custom_cert

router = APIRouter(prefix="/api/tls", tags=["tls"])

MAX_CERT_UPLOAD_BYTES = 256 * 1024


@router.get("/settings", response_model=TlsSettingsOut)
async def get_tls_settings(_: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        return await db.get(TlsSettings, 1)


@router.post("/self-signed", response_model=TlsActionResult)
async def use_self_signed(_: User = Depends(require_admin)):
    generate_self_signed()
    async with AsyncSessionLocal() as db:
        record = await db.get(TlsSettings, 1)
        record.mode = "self_signed"
        record.domain = None
        record.last_renewal_error = None
        await db.commit()
    return TlsActionResult(success=True, message="Self-signed certificate generated. nginx will pick it up within 30s.")


@router.post("/custom", response_model=TlsActionResult)
async def use_custom_cert(
    cert: UploadFile = File(...),
    key: UploadFile = File(...),
    _: User = Depends(require_admin),
):
    cert_bytes = await read_limited(cert, MAX_CERT_UPLOAD_BYTES)
    key_bytes = await read_limited(key, MAX_CERT_UPLOAD_BYTES)
    try:
        save_custom_cert(cert_bytes, key_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    async with AsyncSessionLocal() as db:
        record = await db.get(TlsSettings, 1)
        record.mode = "custom"
        record.domain = None
        record.last_renewal_error = None
        await db.commit()
    return TlsActionResult(success=True, message="Certificate installed. nginx will pick it up within 30s.")


@router.post("/letsencrypt", response_model=TlsActionResult)
async def use_letsencrypt(payload: LetsEncryptRequest, _: User = Depends(require_admin)):
    success, message = await request_letsencrypt_cert(payload.domain, payload.email)
    async with AsyncSessionLocal() as db:
        record = await db.get(TlsSettings, 1)
        if success:
            record.mode = "letsencrypt"
            record.domain = payload.domain
            record.email = payload.email
            record.last_renewal_at = datetime.now(timezone.utc)
            record.last_renewal_error = None
        else:
            record.last_renewal_error = message
        await db.commit()

    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)
    return TlsActionResult(success=True, message=message)
