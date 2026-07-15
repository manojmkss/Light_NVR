import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# httpx/httpcore log every outbound request at INFO, including the full URL.
# For Telegram that URL embeds the bot token (…/bot<TOKEN>/sendMessage), so
# leaving it at INFO writes the secret into the container logs on every alert.
# Raise the floor to WARNING to keep those out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from app.api.routes import ai, auth, backup, cameras, kiosk, live, push, recordings, remote_access, storage, system, tls
from app.core.bootstrap import (
    create_default_admin,
    create_default_ai_settings,
    create_default_alert_settings,
    create_default_backup_settings,
    create_default_discovery_settings,
    create_default_remote_access_settings,
    create_default_storage_config,
    create_default_system_settings,
    create_default_tls_settings,
    ensure_jwt_secret,
    load_security_settings,
)
from app.db.session import check_integrity, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    if not await check_integrity():
        from app.services.events import emit_event

        await emit_event(
            None,
            "system",
            "Database integrity check failed after startup - this usually follows an unclean shutdown "
            "(power loss). Consider restoring from a backup in Settings -> Backup.",
        )

    await ensure_jwt_secret()

    from app.services.web_push import ensure_vapid_keys

    await ensure_vapid_keys()
    await load_security_settings()
    await create_default_admin()
    await create_default_alert_settings()
    await create_default_storage_config()
    await create_default_backup_settings()
    await create_default_tls_settings()
    await create_default_remote_access_settings()
    await create_default_discovery_settings()
    await create_default_system_settings()
    await create_default_ai_settings()

    from app.services import cloudflare_manager, tailscale_manager
    from app.services.camera_supervisor import supervisor
    from app.services.config_backup import backup_loop
    from app.services.db_maintenance import db_maintenance_loop
    from app.services.recovery import cleanup_orphaned_files
    from app.services.retention import retention_loop
    from app.services.storage_manager import storage_manager
    from app.services.storage_mover import storage_mover_loop
    from app.services.tls_manager import cert_expiry_check_loop, ensure_cert_exists, letsencrypt_renewal_loop

    # nginx waits for this container to be healthy before it starts, so the
    # cert is guaranteed to exist by the time nginx's HTTPS listener needs it.
    ensure_cert_exists()

    await storage_manager.start()
    await cleanup_orphaned_files(storage_manager.cache_dir())
    await supervisor.start_all()
    retention_task = asyncio.create_task(retention_loop())
    mover_task = asyncio.create_task(storage_mover_loop())
    backup_task = asyncio.create_task(backup_loop())
    optimize_task = asyncio.create_task(db_maintenance_loop())
    renewal_task = asyncio.create_task(letsencrypt_renewal_loop())
    cert_expiry_task = asyncio.create_task(cert_expiry_check_loop())

    from app.db.session import AsyncSessionLocal
    from app.models.remote_access_settings import RemoteAccessSettings

    async with AsyncSessionLocal() as db:
        remote_access = await db.get(RemoteAccessSettings, 1)
    if remote_access.tailscale_enabled:
        await tailscale_manager.set_enabled(True, remote_access.tailscale_authkey, remote_access.tailscale_hostname)
    if remote_access.cloudflare_enabled:
        await cloudflare_manager.set_enabled(True, remote_access.cloudflare_token)

    yield

    background_tasks = [retention_task, mover_task, backup_task, optimize_task, renewal_task, cert_expiry_task]
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await tailscale_manager.shutdown()
    await cloudflare_manager.shutdown()
    await supervisor.shutdown()
    await storage_manager.shutdown()

    from app.services.hq_stream import hq_stream_manager

    await hq_stream_manager.shutdown()


app = FastAPI(title="LightNVR API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Auth is Bearer-token-based (header or ?token= query param), never
    # cookies, so a wildcard origin is safe here - allow_credentials=True
    # combined with "*" would make browsers treat any origin as trusted for
    # cookie-bearing requests, which this API never relies on.
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ai.router)
app.include_router(auth.router)
app.include_router(backup.router)
app.include_router(cameras.router)
app.include_router(live.router)
app.include_router(push.router)
app.include_router(recordings.router)
app.include_router(remote_access.router)
app.include_router(kiosk.admin_router)
app.include_router(kiosk.public_router)
app.include_router(storage.router)
app.include_router(system.router)
app.include_router(tls.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}
