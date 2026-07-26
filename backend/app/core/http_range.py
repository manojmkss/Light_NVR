"""HTTP Range-aware file serving for seekable <video> playback.

Starlette's FileResponse (the version pinned here) always sends the whole file
with `200 OK` and no `Accept-Ranges` header - so a browser can play a recording
start-to-end but its scrubber is disabled: seeking (especially backward) needs
the server to answer `Range` requests with `206 Partial Content`. This helper
adds that, so recorded clips are seekable in the live and kiosk tiles.
"""

import os

from fastapi import Request
from fastapi.responses import FileResponse, Response, StreamingResponse

_CHUNK = 64 * 1024


def range_file_response(
    request: Request, path: str, media_type: str = "video/mp4", filename: str | None = None
) -> Response:
    """Serve `path`, honoring a single `Range: bytes=...` request header.

    No/!bytes Range -> a normal 200 that advertises `Accept-Ranges: bytes` (so
    the browser knows it *may* seek). A byte range -> `206 Partial Content` with
    just that slice. An unsatisfiable range -> `416`.
    """
    file_size = os.path.getsize(path)
    range_header = request.headers.get("range")

    if not range_header or not range_header.startswith("bytes="):
        resp = FileResponse(path, media_type=media_type, filename=filename)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    # Single range only - <video> never sends multipart ranges. "bytes=start-",
    # "bytes=start-end" and the suffix form "bytes=-N" are all handled.
    try:
        spec = range_header.split("=", 1)[1].split(",", 1)[0].strip()
        start_s, _, end_s = spec.partition("-")
        if start_s == "":
            length = int(end_s)
            if length <= 0:
                raise ValueError
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
        if start > end or start >= file_size:
            raise ValueError
    except (ValueError, IndexError):
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
        )

    end = min(end, file_size - 1)
    length = end - start + 1

    def iter_range():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    if filename:
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return StreamingResponse(iter_range(), status_code=206, media_type=media_type, headers=headers)
