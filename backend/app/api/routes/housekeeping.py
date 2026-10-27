"""Settings -> System -> Health & storage: what uses the disk, the last
clean-up, and what the host guardian has been doing."""

from fastapi import APIRouter, Depends

from app.core.deps import require_admin
from app.models.user import User
from app.services import housekeeping

router = APIRouter(prefix="/api/system/health-storage", tags=["system"])


@router.get("")
async def health_storage(_: User = Depends(require_admin)):
    try:
        breakdown = await housekeeping.disk_breakdown()
    except Exception:
        breakdown = housekeeping.state().get("breakdown") or {}
    st = housekeeping.state()
    guardian = housekeeping.guardian_status()
    return {
        "disk": breakdown,
        "housekeeping": {"last_run": st.get("last_run"), "last": st.get("last") or {}},
        "guardian": guardian,
    }


@router.post("/clean")
async def clean_now(_: User = Depends(require_admin)):
    """'Clean up now' - the full housekeeping pass, plus the free-space floor."""
    from app.services.retention import ensure_free_space

    removed = await ensure_free_space()
    result = await housekeeping.run_housekeeping("by hand")
    result["recordings_removed_for_space"] = removed
    return result
