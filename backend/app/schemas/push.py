from pydantic import BaseModel


class VapidPublicKeyOut(BaseModel):
    public_key: str | None


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeRequest(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys


class UnsubscribeRequest(BaseModel):
    endpoint: str


class PushTestResult(BaseModel):
    success: bool
    message: str
