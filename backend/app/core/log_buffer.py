import logging
import re
from collections import deque

# Ring buffer of recent formatted log lines, served by /api/system/logs so an
# admin can see what the backend is doing without ever needing `docker logs`.
# In-memory only and bounded: survives nothing (by design - it's a live view,
# not an audit trail) and can't grow.
_BUFFER: deque[str] = deque(maxlen=500)

# ffmpeg error output (which gets logged) embeds the full RTSP URL, including
# credentials. Scrub them before anything is stored - these lines are shown in
# the GUI and included in downloadable diagnostics.
_CRED_RE = re.compile(r"(rtsps?://)[^/@\s:]+:[^/@\s]+@")


# Login tokens travel as ?token= on <img>/<video> URLs (they can't send a
# header); never let one land in a log.
_TOKEN_RE = re.compile(r"([?&](?:token|sig)=)[^&\s\"']+")


def scrub_credentials(text: str) -> str:
    return _TOKEN_RE.sub(r"\1***", _CRED_RE.sub(r"\1***:***@", text))


class _AccessLogScrubber(logging.Filter):
    """uvicorn's access log passes the request path as a format arg."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(scrub_credentials(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = scrub_credentials(record.msg)
        return True


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _BUFFER.append(scrub_credentials(self.format(record)))
        except Exception:
            # A logging handler must never take the app down; losing one
            # buffered line is the correct failure mode here.
            pass


def install_log_buffer() -> None:
    """Attach the ring-buffer handler to the root logger. Called from main.py
    at import time - which runs AFTER uvicorn applies its own logging config,
    so this handler survives instead of being wiped by that dictConfig.
    """
    handler = _BufferHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger("uvicorn.access").addFilter(_AccessLogScrubber())


def get_recent_logs(limit: int = 200) -> list[str]:
    return list(_BUFFER)[-limit:]
