from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


async def _resolve_user(token: str, db: AsyncSession) -> User:
    try:
        payload = decode_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials") from exc

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    user_id = payload.get("sub")
    user = await db.get(User, int(user_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    return await _resolve_user(token, db)


async def get_current_user_flexible(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Same as get_current_user, but also accepts a ?token= query param.

    <img>/<video> tags used for live-view snapshots, MJPEG streams, and
    recording playback can't set an Authorization header, so those routes
    depend on this instead.
    """
    token = request.query_params.get("token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return await _resolve_user(token, db)


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


async def require_admin_or_setup_incomplete(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> bool:
    """Allows restoring a config backup either (a) before any admin exists -
    the "new installation" recovery path, where no auth is possible yet - or
    (b) by an authenticated admin on an already-configured instance. Mirrors
    the exact security model POST /api/auth/setup already uses.

    Returns True when this was the anonymous (no-admin-yet) branch, so the
    caller can re-check under a lock immediately before the actual restore -
    this dependency's own check is a fast-path hint, not the final word,
    since two concurrent anonymous calls could otherwise both pass it before
    either commits.
    """
    result = await db.execute(select(User).where(User.role == "admin"))
    if result.scalar_one_or_none() is None:
        return True  # no admin exists yet - anonymous restore allowed, same as /auth/setup

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user = await _resolve_user(auth_header[7:], db)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return False
