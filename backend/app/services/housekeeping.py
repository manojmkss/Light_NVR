"""The NVR's own clean-up, in one place, and its side of the host guardian.

Everything the NVR writes is cleaned up by age or by space:
- recordings, playback copies, events/visits, detections, unknown faces and
  the free-space floor - retention.py, which also calls this module;
- alert pictures (event/visit snapshots) - kept `snapshot_retention_days`,
  set by the user in Settings -> Storage;
- recordings whose file has gone (the guardian deleted it in a disk
  emergency, or it was removed by hand) - their rows are dropped; and
  recordings on disk with no row (a restored backup, an interrupted save) -
  registered again, so nothing on disk is invisible in the app;
- old log files, export leftovers, folders of deleted cameras.

The host guardian (scripts/guardian/) runs outside every container. It and
the NVR talk through files in GUARDIAN_DIR (the `storage/guardian` folder on
the host), so it works whichever side is down:
- guardian -> NVR: `outbox/*.json` messages (a recovery, a clean-up), sent
  as alerts here;
- guardian -> NVR: `status.json` (its checks, recoveries, host disk use),
  shown on the Health & storage card;
- NVR -> guardian: `expect.json` - which cameras record continuously and
  are online, and when each last made a recording, so it can tell "recording
  is stuck" from "nothing to record"; and where recordings are kept, for its
  last-resort deletes when the NVR is down and the disk is full (the list of
  files it deleted comes back in its message, and those rows are dropped).
"""

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.camera import Camera
from app.models.detection import Detection
from app.models.event import Event
from app.models.incident import Incident
from app.models.recording import Recording
from app.models.storage_config import StorageConfig
from app.services.events import emit_event
from app.services.storage_manager import storage_manager

logger = logging.getLogger(__name__)

GUARDIAN_DIR = os.environ.get("LIGHTNVR_GUARDIAN_DIR", "/storage/guardian")
STATE_FILE = os.environ.get("LIGHTNVR_HOUSEKEEPING_FILE", "/data/housekeeping.json")
BRIDGE_SECONDS = 30
CLEAN_EVERY_SECONDS = 6 * 3600
LOG_MAX_AGE_DAYS = 14
ORPHAN_MIN_AGE_SECONDS = 600  # a file this new may still be on its way in
BATCH = 500

_state: dict = {"last_run": None, "last": {}, "breakdown": {}}
_run_lock = asyncio.Lock()


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _remove(paths) -> tuple[int, int]:
    files = size = 0
    for p in paths:
        try:
            n = os.path.getsize(p)
            os.remove(p)
            files += 1
            size += n
        except OSError:
            pass
    return files, size


# ── alert pictures ──────────────────────────────────────────────────────────


async def prune_snapshots(days: int) -> tuple[int, int]:
    """Delete event and visit pictures older than `days`; the events and
    visits themselves stay. A picture a detection still uses is kept."""
    cutoff = _naive_now() - timedelta(days=max(1, days))
    files = size = 0
    async with AsyncSessionLocal() as db:
        for model, when in ((Event, Event.created_at), (Incident, Incident.started_at)):
            rows = (await db.execute(
                select(model).where(when < cutoff, model.snapshot_path.is_not(None)).limit(5000)
            )).scalars().all()
            if not rows:
                continue
            paths = {r.snapshot_path for r in rows}
            shared = set((await db.execute(
                select(Detection.snapshot_path).where(Detection.snapshot_path.in_(paths))
            )).scalars().all())
            f, s = await asyncio.to_thread(_remove, sorted(paths - shared))
            files, size = files + f, size + s
            for r in rows:
                if r.snapshot_path not in shared:
                    r.snapshot_path = None
        # The visit review's people photos (cache/visits/<visit>/).
        root = os.path.join(storage_manager.cache_dir(), "visits")
        try:
            on_disk = [int(d) for d in os.listdir(root) if d.isdigit()]
        except OSError:
            on_disk = []
        old = (await db.execute(select(Incident).where(Incident.id.in_(on_disk[:5000]), Incident.started_at < cutoff)
                                )).scalars().all() if on_disk else []
        if old:
            f, s = await asyncio.to_thread(lambda: _remove_dirs([os.path.join(root, str(r.id)) for r in old]))
            files, size = files + f, size + s
            for r in old:
                r.people = [{**p, "photo": None, "full": None} for p in r.people or []] or None
        if on_disk:
            # Folders of visits that no longer exist (history kept less long).
            known = set((await db.execute(select(Incident.id).where(Incident.id.in_(on_disk[:5000])))).scalars().all())
            gone = [os.path.join(root, str(i)) for i in on_disk[:5000] if i not in known]
            if gone:
                f, s = await asyncio.to_thread(_remove_dirs, gone)
                files, size = files + f, size + s
        await db.commit()
    return files, size


def _remove_dirs(dirs: list[str]) -> tuple[int, int]:
    files = size = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        f, s = _remove([os.path.join(d, n) for n in os.listdir(d)])
        files, size = files + f, size + s
        shutil.rmtree(d, ignore_errors=True)
    return files, size


# ── recordings <-> files ────────────────────────────────────────────────────


def _tier_dirs() -> dict[str, str]:
    dirs = {}
    if storage_manager.is_primary_available():
        dirs["primary"] = storage_manager.primary_dir()
    if storage_manager.is_backup_available() and storage_manager.backup_dir():
        dirs["backup"] = storage_manager.backup_dir()
    return dirs


async def reconcile() -> dict:
    """Rows whose file has gone are dropped; recordings on disk with no row
    are registered again. Only for storage that is reachable right now (a
    NAS that's down must never read as "every file is gone"), and never the
    cache - the storage mover is busy moving files out of it."""
    from app.services.retention import purge_recording

    dirs = _tier_dirs()
    dropped = registered = 0
    for tier, base in dirs.items():
        # 1) rows -> files
        last_id = 0
        while True:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(Recording).where(Recording.storage_tier == tier, Recording.id > last_id)
                    .order_by(Recording.id).limit(BATCH)
                )).scalars().all()
                if not rows:
                    break
                last_id = rows[-1].id
                exists = await asyncio.wait_for(
                    asyncio.to_thread(lambda: {r.id: os.path.exists(r.file_path) for r in rows}), timeout=120)
                gone = [r for r in rows if not exists[r.id]]
                if len(gone) > 20 and len(gone) > len(rows) // 5:
                    # Whole swathes missing at once: the disk isn't mounted (an
                    # empty mount point), not a few files deleted. Touch nothing.
                    logger.error("Housekeeping: %d of %d %s recordings missing - storage looks unmounted; "
                                 "not dropping anything", len(gone), len(rows), tier)
                    await emit_event(None, "system", f"{len(gone)} of {len(rows)} recordings checked are missing from "
                                     f"{tier} storage - is the disk mounted? Nothing was removed.")
                    break
                for r in gone:
                    await purge_recording(db, r)
                if gone:
                    await db.commit()
                    dropped += len(gone)
        # 2) files -> rows
        registered += await _register_orphans(tier, base)
    if dropped or registered:
        logger.info("Housekeeping: dropped %d recording(s) whose file had gone, registered %d found on disk",
                    dropped, registered)
    return {"dropped": dropped, "registered": registered}


async def _register_orphans(tier: str, base: str) -> int:
    from app.services.ffmpeg_recorder import footage_start, probe_duration
    from app.services.recovery import _recorded_at

    async with AsyncSessionLocal() as db:
        known = set()
        for f, t, p in (await db.execute(
            select(Recording.file_path, Recording.thumbnail_path, Recording.playback_path)
            .where(Recording.storage_tier == tier)
        )).all():
            known.update(x for x in (f, t, p) if x)
        cameras = {c.id: c.codec for c in (await db.execute(select(Camera))).scalars().all()}

    def scan() -> list[tuple[int, str]]:
        out = []
        now = time.time()
        for d in os.listdir(base):
            if not d.isdigit() or int(d) not in cameras:
                continue
            for name in os.listdir(os.path.join(base, d)):
                path = os.path.join(base, d, name)
                if (name.endswith(".mp4") and not name.endswith(".sub.mp4") and path not in known
                        and now - os.path.getmtime(path) > ORPHAN_MIN_AGE_SECONDS):
                    out.append((int(d), path))
        return out[:300]

    try:
        found = await asyncio.wait_for(asyncio.to_thread(scan), timeout=120)
    except (OSError, asyncio.TimeoutError):
        return 0
    n = 0
    for camera_id, path in found:
        duration = await probe_duration(path)
        if not duration:
            continue
        ended = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        named, trigger = _recorded_at(os.path.basename(path))
        started = footage_start(named, ended, duration) if named else ended - timedelta(seconds=duration)
        thumb = os.path.splitext(path)[0] + ".jpg"
        async with AsyncSessionLocal() as db:
            db.add(Recording(camera_id=camera_id, file_path=path, thumbnail_path=thumb if os.path.exists(thumb) else None,
                             trigger=trigger, started_at=started, ended_at=ended, duration_seconds=duration,
                             size_bytes=os.path.getsize(path), codec=cameras.get(camera_id), storage_tier=tier))
            await db.commit()
        n += 1
    return n


# ── logs and debris ─────────────────────────────────────────────────────────


def _old_files(directory: str, max_age_s: float, prefix: str = "") -> list[str]:
    try:
        now = time.time()
        return [os.path.join(directory, n) for n in os.listdir(directory)
                if n.startswith(prefix) and os.path.isfile(os.path.join(directory, n))
                and now - os.path.getmtime(os.path.join(directory, n)) > max_age_s]
    except OSError:
        return []


async def prune_debris() -> tuple[int, int]:
    """Old rotated logs, clip-export leftovers a crash left in /tmp, and the
    folders of cameras that no longer exist."""
    from app.core.log_buffer import LOG_DIR

    files = size = 0
    for paths in (_old_files(LOG_DIR, LOG_MAX_AGE_DAYS * 86400), _old_files("/tmp", 3600, "clip_")):
        f, s = await asyncio.to_thread(_remove, paths)
        files, size = files + f, size + s

    async with AsyncSessionLocal() as db:
        cameras = {str(c) for c in (await db.execute(select(Camera.id))).scalars().all()}
        with_rows = {str(c) for c in (await db.execute(select(Recording.camera_id).distinct())).scalars().all()}

    def drop_dirs() -> tuple[int, int]:
        f = s = 0
        # (Not events/: a deleted camera's alert pictures follow their own retention.)
        roots = [storage_manager.cache_dir(), *_tier_dirs().values()]
        for root in roots:
            try:
                names = os.listdir(root)
            except OSError:
                continue
            for d in names:
                path = os.path.join(root, d)
                if d.isdigit() and d not in cameras and d not in with_rows and os.path.isdir(path):
                    for dirpath, _, filenames in os.walk(path):
                        for n in filenames:
                            try:
                                s += os.path.getsize(os.path.join(dirpath, n))
                                f += 1
                            except OSError:
                                pass
                    shutil.rmtree(path, ignore_errors=True)
        return f, s

    f, s = await asyncio.wait_for(asyncio.to_thread(drop_dirs), timeout=300)
    return files + f, size + s


# ── what uses the disk ──────────────────────────────────────────────────────


def _du(path: str) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for n in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
            except OSError:
                pass
    return total


async def disk_breakdown() -> dict:
    from app.core.log_buffer import LOG_DIR

    target = storage_manager.primary_dir() if storage_manager.is_primary_available() else storage_manager.cache_dir()
    usage = await asyncio.to_thread(shutil.disk_usage, target)
    async with AsyncSessionLocal() as db:
        recordings = sum(r or 0 for r in (await db.execute(select(Recording.size_bytes))).scalars().all())
    cache = storage_manager.cache_dir()
    pictures = await asyncio.to_thread(
        lambda: sum(_du(os.path.join(cache, d)) for d in ("events", "detections", "faces", "visits")))
    logs = await asyncio.to_thread(_du, LOG_DIR)
    return {"total": usage.total, "used": usage.used, "free": usage.free, "recordings": recordings,
            "pictures": pictures, "logs": logs}


# ── one clean-up pass ───────────────────────────────────────────────────────


async def run_housekeeping(reason: str = "scheduled") -> dict:
    """Everything above, once. Each part is isolated: one failing never stops
    the others."""
    async with _run_lock:
        started = time.monotonic()
        async with AsyncSessionLocal() as db:
            cfg = await db.get(StorageConfig, 1)
        days = getattr(cfg, "snapshot_retention_days", None) or 30
        result: dict = {"reason": reason}
        for name, coro in (("pictures", prune_snapshots(days)), ("debris", prune_debris())):
            try:
                f, s = await coro
                result[name] = {"files": f, "bytes": s}
            except Exception as exc:
                logger.exception("Housekeeping: %s failed", name)
                result[name] = {"error": str(exc)[:200]}
        try:
            result["reconcile"] = await reconcile()
        except Exception as exc:
            logger.exception("Housekeeping: reconcile failed")
            result["reconcile"] = {"error": str(exc)[:200]}
        try:
            _state["breakdown"] = await disk_breakdown()
        except Exception:
            logger.exception("Housekeeping: disk breakdown failed")
        result["seconds"] = round(time.monotonic() - started, 1)
        freed = sum(v.get("bytes", 0) for v in result.values() if isinstance(v, dict))
        result["freed_bytes"] = freed
        _state.update(last_run=datetime.now(timezone.utc).isoformat(), last=result)
        _save_state()
        logger.info("Housekeeping (%s): freed %.1f MB in %ss", reason, freed / 1e6, result["seconds"])
        return result


def _save_state() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def _load_state() -> None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            _state.update(json.load(f))
    except (OSError, ValueError):
        pass


def state() -> dict:
    return dict(_state)


# ── the host guardian's side ────────────────────────────────────────────────


def guardian_status() -> dict | None:
    """What the guardian last wrote, and whether it's still writing."""
    try:
        with open(os.path.join(GUARDIAN_DIR, "status.json"), encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, ValueError):
        return None
    status["fresh"] = time.time() - os.path.getmtime(os.path.join(GUARDIAN_DIR, "status.json")) < 180
    return status


async def write_expectations() -> None:
    async with AsyncSessionLocal() as db:
        cams = (await db.execute(select(Camera.id, Camera.status).where(
            Camera.enabled.is_(True), Camera.recording_mode == "continuous"))).all()
        ids = [c for c, _ in cams]
        last = dict((await db.execute(
            select(Recording.camera_id, func.max(Recording.ended_at))
            .where(Recording.camera_id.in_(ids)).group_by(Recording.camera_id)
        )).all()) if ids else {}
    data = {
        "at": time.time(),
        "continuous_cameras": sorted(ids),
        "online_cameras": sorted(c for c, status in cams if status == "online"),
        "last_recording": {str(c): t.replace(tzinfo=timezone.utc).timestamp() for c, t in last.items() if t},
        "recording_dirs": {"cache": storage_manager.cache_dir(), **_tier_dirs()},
    }

    def write():
        os.makedirs(GUARDIAN_DIR, exist_ok=True)
        tmp = os.path.join(GUARDIAN_DIR, "expect.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(GUARDIAN_DIR, "expect.json"))

    await asyncio.to_thread(write)


async def _drop_deleted(paths: list[str]) -> int:
    """Rows of the recordings the guardian deleted while the NVR was down."""
    from app.services.retention import purge_recording

    n = 0
    for i in range(0, len(paths), BATCH):
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Recording).where(Recording.file_path.in_(paths[i:i + BATCH])))).scalars().all()
            for r in rows:
                await purge_recording(db, r)
            await db.commit()
            n += len(rows)
    return n


async def ingest_guardian_messages() -> int:
    """The guardian's messages become events (and alerts)."""
    outbox = os.path.join(GUARDIAN_DIR, "outbox")
    try:
        names = sorted(n for n in os.listdir(outbox) if n.endswith(".json"))
    except OSError:
        return 0
    n = 0
    for name in names[:50]:
        path = os.path.join(outbox, name)
        try:
            with open(path, encoding="utf-8") as f:
                msg = json.load(f)
            deleted = [p for p in msg.pop("deleted_files", None) or [] if isinstance(p, str)]
            if deleted:
                dropped = await _drop_deleted(deleted)
                logger.info("Housekeeping: guardian deleted %d recording(s); dropped %d row(s)", len(deleted), dropped)
            text = str(msg.get("message", ""))[:900]
            if text:
                await emit_event(None, "guardian", text, payload={k: v for k, v in msg.items() if k != "message"},
                                 notify=bool(msg.get("notify", True)))
                n += 1
        except (OSError, ValueError):
            logger.warning("Housekeeping: unreadable guardian message %s - dropped", name)
        try:
            os.remove(path)
        except OSError:
            pass
    return n


FIRST_CLEAN_AFTER_SECONDS = 600  # not in the busy first minutes after a start


async def housekeeping_loop() -> None:
    _load_state()
    last_clean = time.monotonic() - CLEAN_EVERY_SECONDS + FIRST_CLEAN_AFTER_SECONDS
    while True:
        for step in (write_expectations, ingest_guardian_messages):
            try:
                await step()
            except Exception:
                logger.exception("Housekeeping: %s failed", step.__name__)
        if time.monotonic() - last_clean > CLEAN_EVERY_SECONDS:
            last_clean = time.monotonic()
            try:
                await run_housekeeping()
            except Exception:
                logger.exception("Housekeeping pass failed")
        await asyncio.sleep(BRIDGE_SECONDS)
