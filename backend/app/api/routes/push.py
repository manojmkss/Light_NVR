from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.push_subscription import PushSubscription
from app.models.user import User
from app.schemas.push import PushTestResult, SubscribeRequest, UnsubscribeRequest, VapidPublicKeyOut
from app.services.web_push import get_vapid_public_key, send_push_to_subscription

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid-public-key", response_model=VapidPublicKeyOut)
async def vapid_public_key(_: User = Depends(get_current_user)):
    return VapidPublicKeyOut(public_key=await get_vapid_public_key())


@router.post("/subscribe", status_code=204)
async def subscribe(
    payload: SubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint))
    record = existing.scalar_one_or_none()
    if record is None:
        record = PushSubscription(endpoint=payload.endpoint, user_id=user.id, p256dh=payload.keys.p256dh, auth=payload.keys.auth)
        db.add(record)
    else:
        record.user_id = user.id
        record.p256dh = payload.keys.p256dh
        record.auth = payload.keys.auth
    await db.commit()


@router.post("/unsubscribe", status_code=204)
async def unsubscribe(
    payload: UnsubscribeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint, PushSubscription.user_id == user.id)
    )
    record = existing.scalar_one_or_none()
    if record is not None:
        await db.delete(record)
        await db.commit()


@router.post("/test", response_model=PushTestResult)
async def test_push(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PushSubscription).where(PushSubscription.user_id == user.id))
    subscriptions = result.scalars().all()
    if not subscriptions:
        return PushTestResult(success=False, message="No push subscriptions on this account yet")

    sent = 0
    last_error = ""
    for sub in subscriptions:
        success, detail = await send_push_to_subscription(sub, "LightNVR test", "This is a test push notification.")
        if success:
            sent += 1
        else:
            last_error = detail

    if sent:
        return PushTestResult(success=True, message=f"Sent to {sent} of {len(subscriptions)} device(s)")
    return PushTestResult(success=False, message=last_error or "Failed to send")
