import logging
import logging.handlers
import os
import re
import sys
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


class _ScrubbingFormatter(logging.Formatter):
    """Credentials and tokens never reach a log, whichever logger wrote it."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub_credentials(super().format(record))


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DIR = os.environ.get("LIGHTNVR_LOG_DIR", "/data/logs")


def install_log_buffer() -> None:
    """Where the backend's own messages go. Called from main.py at import
    time - which runs AFTER uvicorn applies its own logging config, so these
    handlers survive instead of being wiped by that dictConfig.

    - the in-memory ring buffer (Settings -> System logs);
    - stdout, so `docker logs` shows them (before this, only uvicorn's own
      lines ever reached it: every warning and error was invisible there);
    - a rotating file in the data volume (5 x 10 MB) that survives updates,
      which replace the container and its Docker log with it.
    """
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    handler = _BufferHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.INFO)
    out.setFormatter(_ScrubbingFormatter(LOG_FORMAT))
    root.addHandler(out)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        to_file = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "lightnvr.log"), maxBytes=10 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
        to_file.setLevel(logging.INFO)
        to_file.setFormatter(_ScrubbingFormatter(LOG_FORMAT))
        root.addHandler(to_file)
        # uvicorn's own errors don't reach the root logger; keep them too.
        logging.getLogger("uvicorn.error").addHandler(to_file)
    except OSError:
        pass  # no writable data dir (tests, a read-only dev checkout): stdout still has it all
    logging.getLogger("uvicorn.access").addFilter(_AccessLogScrubber())


def get_recent_logs(limit: int = 200) -> list[str]:
    return list(_BUFFER)[-limit:]
