from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    email: str | None = None

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "viewer"
    email: str | None = None


class SetupStatusOut(BaseModel):
    setup_required: bool


class SetupRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


class SecuritySettingsOut(BaseModel):
    access_token_expire_minutes: int
    refresh_token_expire_days: int

    class Config:
        from_attributes = True


class SecuritySettingsUpdate(BaseModel):
    access_token_expire_minutes: int | None = None
    refresh_token_expire_days: int | None = None
