import secrets

from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.alert_settings import AlertSettings
from app.models.backup_settings import BackupSettings
from app.models.discovery_settings import DiscoverySettings
from app.models.remote_access_settings import RemoteAccessSettings
from app.models.system_settings import SystemSettings
from app.models.security_settings import SecuritySettings
from app.models.storage_config import StorageConfig
from app.models.system_secret import SystemSecret
from app.models.tls_settings import TlsSettings
from app.models.user import User


async def ensure_jwt_secret() -> None:
    """Must run before any JWT is created or verified. Respects an explicit
    JWT_SECRET_KEY env var (scripted/multi-instance deployments); otherwise
    generates one on first boot and persists it so every restart reuses the
    same secret instead of invalidating every session.
    """
    if settings.jwt_secret_key:
        return

    async with AsyncSessionLocal() as db:
        record = await db.get(SystemSecret, 1)
        if record is not None and record.jwt_secret:
            settings.jwt_secret_key = record.jwt_secret
            return

        generated = secrets.token_urlsafe(48)
        if record is None:
            db.add(SystemSecret(id=1, jwt_secret=generated))
        else:
            record.jwt_secret = generated
        await db.commit()
        settings.jwt_secret_key = generated


async def create_default_admin() -> None:
    """Optional unattended bootstrap. If ADMIN_USERNAME/ADMIN_PASSWORD
    aren't both set, no admin is created here - the GUI setup wizard
    (POST /api/auth/setup) handles first-run account creation instead.
    """
    if not settings.admin_username or not settings.admin_password:
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.role == "admin"))
        if result.scalar_one_or_none() is not None:
            return

        admin = User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            role="admin",
        )
        db.add(admin)
        await db.commit()


async def create_default_alert_settings() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.get(AlertSettings, 1)
        if existing is not None:
            return
        db.add(AlertSettings(id=1))
        await db.commit()


async def create_default_storage_config() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.get(StorageConfig, 1)
        if existing is not None:
            return
        db.add(StorageConfig(id=1))
        await db.commit()


async def create_default_backup_settings() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.get(BackupSettings, 1)
        if existing is not None:
            return
        db.add(BackupSettings(id=1))
        await db.commit()


async def create_default_tls_settings() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.get(TlsSettings, 1)
        if existing is not None:
            return
        db.add(TlsSettings(id=1))
        await db.commit()


async def create_default_remote_access_settings() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.get(RemoteAccessSettings, 1)
        if existing is not None:
            return
        db.add(RemoteAccessSettings(id=1))
        await db.commit()


async def create_default_discovery_settings() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.get(DiscoverySettings, 1)
        if existing is not None:
            return
        db.add(DiscoverySettings(id=1))
        await db.commit()


async def create_default_system_settings() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.get(SystemSettings, 1)
        if existing is not None:
            return
        db.add(SystemSettings(id=1))
        await db.commit()


async def load_security_settings() -> None:
    """Creates the default row on first boot, and on every call after
    syncs SecuritySettings into the mutable `settings` singleton that
    app.core.security actually reads token expiry from - the same pattern
    as ensure_jwt_secret, so the Settings -> Account UI can change these
    without a restart.
    """
    async with AsyncSessionLocal() as db:
        record = await db.get(SecuritySettings, 1)
        if record is None:
            record = SecuritySettings(id=1)
            db.add(record)
            await db.commit()
            await db.refresh(record)

        settings.jwt_access_token_expire_minutes = record.access_token_expire_minutes
        settings.jwt_refresh_token_expire_days = record.refresh_token_expire_days
