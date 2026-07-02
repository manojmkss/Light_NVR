import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.push_subscription import PushSubscription
from app.models.system_secret import SystemSecret

logger = logging.getLogger(__name__)

VAPID_CLAIM_SUB = "mailto:admin@lightnvr.local"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_vapid_keypair() -> tuple[str, str]:
    """Returns (public_key, private_key) as the base64url-encoded raw forms
    Web Push and pywebpush both expect - the public key as an uncompressed
    SEC1 EC point, the private key as a raw 32-byte big-endian integer.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_value = private_key.private_numbers().private_value
    private_bytes = private_value.to_bytes(32, "big")

    public_numbers = private_key.public_key().public_numbers()
    public_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")

    return _b64url(public_bytes), _b64url(private_bytes)


async def ensure_vapid_keys() -> None:
    """Generated once on first boot, same pattern as ensure_jwt_secret -
    no key management required from the user.
    """
    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSecret, 1)
        if record is not None and record.vapid_public_key and record.vapid_private_key:
            return

        public_key, private_key = generate_vapid_keypair()
        if record is None:
            db.add(SystemSecret(id=1, vapid_public_key=public_key, vapid_private_key=private_key))
        else:
            record.vapid_public_key = public_key
            record.vapid_private_key = private_key
        await db.commit()


async def get_vapid_public_key() -> str | None:
    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSecret, 1)
        return record.vapid_public_key if record else None


async def send_push_to_subscription(subscription: PushSubscription, title: str, body: str) -> tuple[bool, str]:
    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSecret, 1)
    if not record or not record.vapid_private_key:
        return False, "VAPID keys not configured"

    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
    }

    def _send():
        webpush(
            subscription_info=subscription_info,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=record.vapid_private_key,
            vapid_claims={"sub": VAPID_CLAIM_SUB},
        )

    try:
        await asyncio.to_thread(_send)
        return True, "Sent"
    except WebPushException as exc:
        # 404/410 means the subscription is gone (browser data cleared,
        # uninstalled, etc.) - the caller cleans these up rather than
        # retrying something that will never succeed again.
        status_code = exc.response.status_code if exc.response is not None else None
        return False, f"expired:{status_code}" if status_code in (404, 410) else str(exc)


async def send_push_to_all(title: str, body: str) -> None:
    async with AsyncSessionLocal() as db:
        subscriptions = (await db.execute(select(PushSubscription))).scalars().all()

    expired_ids = []
    for sub in subscriptions:
        success, detail = await send_push_to_subscription(sub, title, body)
        if not success:
            logger.warning("Push delivery failed for subscription %d: %s", sub.id, detail)
            if detail.startswith("expired:"):
                expired_ids.append(sub.id)

    if expired_ids:
        async with AsyncSessionLocal() as db:
            for sub_id in expired_ids:
                sub = await db.get(PushSubscription, sub_id)
                if sub:
                    await db.delete(sub)
            await db.commit()
