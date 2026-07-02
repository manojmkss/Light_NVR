from datetime import datetime

from pydantic import BaseModel


class TlsSettingsOut(BaseModel):
    mode: str
    domain: str | None
    email: str | None
    last_renewal_at: datetime | None
    last_renewal_error: str | None

    class Config:
        from_attributes = True


class LetsEncryptRequest(BaseModel):
    domain: str
    email: str


class TlsActionResult(BaseModel):
    success: bool
    message: str
