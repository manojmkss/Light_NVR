import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.db.session import AsyncSessionLocal, get_db
from app.models.ai_settings import AISettings
from app.models.detection import Detection
from app.models.user import User
from app.schemas.ai import AISettingsOut, AISettingsUpdate, AITestResult, DetectionOut
from app.services.ai.pipeline import ai_pipeline
from app.services.ai.types import COCO_CLASSES, SUGGESTED_CLASSES

router = APIRouter(prefix="/api/ai", tags=["ai"])


def _to_out(record: AISettings) -> AISettingsOut:
    return AISettingsOut(
        enabled=record.enabled,
        backend=record.backend,
        remote_url=record.remote_url,
        has_remote_api_key=bool(record.remote_api_key),
        detection_enabled=record.detection_enabled,
        detection_model=record.detection_model,
        detection_confidence=record.detection_confidence,
        detection_classes=[c for c in record.detection_classes.split(",") if c],
        alert_on_objects_only=record.alert_on_objects_only,
        search_enabled=record.search_enabled,
        embedding_model=record.embedding_model,
        vlm_enabled=record.vlm_enabled,
        vlm_provider=record.vlm_provider,
        vlm_url=record.vlm_url,
        vlm_model=record.vlm_model,
        has_vlm_api_key=bool(record.vlm_api_key),
        vlm_daily_digest=record.vlm_daily_digest,
        privacy_ack=record.privacy_ack,
        face_enabled=record.face_enabled,
        face_threshold=record.face_threshold,
        alpr_enabled=record.alpr_enabled,
        detection_retention_days=record.detection_retention_days,
    )


@router.get("/classes")
async def list_classes(_: User = Depends(require_admin)):
    """Everything the detector can emit, plus the shortlist worth showing first
    (an NVR operator does not need to scroll past 'toaster')."""
    return {"all": COCO_CLASSES, "suggested": SUGGESTED_CLASSES}


@router.get("/settings", response_model=AISettingsOut)
async def get_ai_settings(_: User = Depends(require_admin)):
    async with AsyncSessionLocal() as db:
        record = await db.get(AISettings, 1)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI settings not initialised")
        return _to_out(record)


@router.put("/settings", response_model=AISettingsOut)
async def update_ai_settings(payload: AISettingsUpdate, _: User = Depends(require_admin)):
    data = payload.model_dump(exclude_unset=True)

    async with AsyncSessionLocal() as db:
        record = await db.get(AISettings, 1)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="AI settings not initialised")

        # Secrets: absent means "leave alone", empty string means "clear it".
        # Without this distinction the UI (which never receives the stored key
        # back) would wipe it on every unrelated save.
        for secret in ("remote_api_key", "vlm_api_key"):
            if secret in data and data[secret] is None:
                data.pop(secret)

        if "detection_classes" in data and data["detection_classes"] is not None:
            data["detection_classes"] = ",".join(
                c.strip().lower() for c in data["detection_classes"] if c and c.strip()
            )

        merged_backend = data.get("backend", record.backend)
        merged_remote_url = data.get("remote_url", record.remote_url)
        if merged_backend == "remote" and not merged_remote_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A remote AI worker URL is required when the backend is set to 'remote'",
            )

        # Face recognition and ALPR process other people's biometric /
        # identifying data. Enforce the acknowledgement server-side too - a UI
        # checkbox alone would be bypassable by anyone calling the API directly.
        wants_recognition = data.get("face_enabled", record.face_enabled) or data.get(
            "alpr_enabled", record.alpr_enabled
        )
        has_ack = data.get("privacy_ack", record.privacy_ack)
        if wants_recognition and not has_ack:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Face recognition and licence-plate recognition process personal data and "
                    "must be acknowledged (privacy_ack) before they can be enabled."
                ),
            )

        for field, value in data.items():
            setattr(record, field, value)
        await db.commit()
        await db.refresh(record)
        return _to_out(record)


@router.post("/test", response_model=AITestResult)
async def test_ai_backend(_: User = Depends(require_admin)):
    """Proves the configured backend is actually reachable/loadable before the
    user finds out via silently missing alerts at 3am."""
    started = time.monotonic()
    try:
        ok, detail, backend = await ai_pipeline.health()
    except Exception as exc:
        return AITestResult(success=False, message=str(exc))
    return AITestResult(
        success=ok,
        message=detail,
        backend=backend,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


@router.get("/detections", response_model=list[DetectionOut])
async def list_detections(
    camera_id: int | None = None,
    label: str | None = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Object-filtered history - the Tier 1 half of search ('all vehicles
    yesterday'). Semantic text search arrives with Tier 2."""
    stmt = select(Detection).order_by(Detection.created_at.desc())
    if camera_id is not None:
        stmt = stmt.where(Detection.camera_id == camera_id)
    if label:
        stmt = stmt.where(Detection.label == label.lower())
    result = await db.execute(stmt.offset(offset).limit(limit))
    return result.scalars().all()
