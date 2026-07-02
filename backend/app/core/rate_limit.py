import time
from collections import defaultdict

from fastapi import HTTPException, status

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300
LOCKOUT_SECONDS = 300

_failures: dict[str, list[float]] = defaultdict(list)


def check_lockout(key: str) -> None:
    """In-memory only - resets on restart, which is fine for a single-process
    self-hosted instance and avoids pulling in Redis/slowapi for one feature.
    """
    now = time.monotonic()
    attempts = _failures[key]
    attempts[:] = [t for t in attempts if now - t < WINDOW_SECONDS]
    if len(attempts) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts - try again in {LOCKOUT_SECONDS // 60} minutes",
        )


def record_failure(key: str) -> None:
    _failures[key].append(time.monotonic())


def clear(key: str) -> None:
    _failures.pop(key, None)
