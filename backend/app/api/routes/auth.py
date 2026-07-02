import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.bootstrap import load_security_settings
from app.core.deps import get_current_user, require_admin
from app.core.rate_limit import check_lockout, clear, record_failure
from app.core.security import create_token, decode_token, hash_password, verify_password
from app.db.session import get_db
from app.models.security_settings import SecuritySettings
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    SecuritySettingsOut,
    SecuritySettingsUpdate,
    SetupRequest,
    SetupStatusOut,
    TokenResponse,
    UserCreate,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8

# Serializes the entire check-then-create window for /setup and the
# anonymous branch of /backup/restore-upload - both allow an unauthenticated
# caller to act only while no admin exists yet, and without this lock two
# concurrent requests could each pass that check before either commits.
setup_lock = asyncio.Lock()


def _validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )


def _client_ip(request: Request) -> str:
    # Trusts X-Real-IP only because our own nginx sets it; falls back to the
    # direct peer address for callers hitting the backend port directly.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


@router.get("/setup-status", response_model=SetupStatusOut)
async def setup_status(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.role == "admin"))
    return SetupStatusOut(setup_required=result.scalar_one_or_none() is None)


@router.post("/setup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def setup(payload: SetupRequest, db: AsyncSession = Depends(get_db)):
    """Creates the first admin account through the browser, no .env editing
    or guessable default credentials required. Only works once - if an admin
    already exists, this is rejected so it can't be used to inject a new
    unauthorized admin into an already-configured instance.
    """
    async with setup_lock:
        result = await db.execute(select(User).where(User.role == "admin"))
        if result.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Setup has already been completed")

        if not payload.username.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username is required")
        _validate_password_strength(payload.password)

        existing = await db.execute(select(User).where(func.lower(User.username) == payload.username.lower()))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

        admin = User(username=payload.username, password_hash=hash_password(payload.password), role="admin")
        db.add(admin)
        await db.commit()
        await db.refresh(admin)

    access_token = create_token(str(admin.id), admin.role, "access")
    refresh_token = create_token(str(admin.id), admin.role, "refresh")
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    key = f"{_client_ip(request)}:{payload.username.lower()}"
    check_lockout(key)

    result = await db.execute(select(User).where(func.lower(User.username) == payload.username.lower()))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    clear(key)

    access_token = create_token(str(user.id), user.role, "access")
    refresh_token = create_token(str(user.id), user.role, "refresh")
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # Rate-limited per client IP, same mechanism as /login, so a flood of
    # invalid refresh attempts can't be used to hammer the endpoint. A valid
    # refresh clears the counter.
    key = f"refresh:{_client_ip(request)}"
    check_lockout(key)

    try:
        decoded = decode_token(payload.refresh_token)
    except ValueError as exc:
        record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc

    if decoded.get("type") != "refresh":
        record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    user = await db.get(User, int(decoded["sub"]))
    if user is None:
        record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    clear(key)
    access_token = create_token(str(user.id), user.role, "access")
    new_refresh_token = create_token(str(user.id), user.role, "refresh")
    return TokenResponse(access_token=access_token, refresh_token=new_refresh_token)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect")
    _validate_password_strength(payload.new_password)

    db_user = await db.get(User, user.id)
    db_user.password_hash = hash_password(payload.new_password)
    await db.commit()


@router.get("/users", response_model=list[UserOut])
async def list_users(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User))
    return result.scalars().all()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(func.lower(User.username) == payload.username.lower()))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    if payload.role not in ("admin", "viewer"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Role must be 'admin' or 'viewer'")
    _validate_password_strength(payload.password)

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        email=payload.email,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.put("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_user_password(
    user_id: int,
    payload: ResetPasswordRequest,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Lets an admin reset another account's password without knowing the
    old one - covers the "forgot password" case without needing a full
    email-based recovery flow.
    """
    _validate_password_strength(payload.new_password)
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.password_hash = hash_password(payload.new_password)
    await db.commit()


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own account")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await db.delete(user)
    await db.commit()


@router.get("/security-settings", response_model=SecuritySettingsOut)
async def get_security_settings(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)):
    return await db.get(SecuritySettings, 1)


@router.put("/security-settings", response_model=SecuritySettingsOut)
async def update_security_settings(
    payload: SecuritySettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    record = await db.get(SecuritySettings, 1)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(record, field, value)
    await db.commit()
    await db.refresh(record)

    await load_security_settings()  # sync into the in-memory settings object immediately, no restart needed
    return record
