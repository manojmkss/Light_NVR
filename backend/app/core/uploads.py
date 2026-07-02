from fastapi import HTTPException, UploadFile, status

CHUNK_SIZE = 64 * 1024


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    """Reads an upload incrementally so an oversized body is rejected before
    it's fully buffered in memory - plain `await file.read()` has no size
    cap and will happily buffer a multi-GB upload, which is exploitable from
    any caller that can reach the backend (including bypassing nginx by
    hitting its exposed port directly).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            limit_label = f"{max_bytes // (1024 * 1024)}MB" if max_bytes >= 1024 * 1024 else f"{max_bytes // 1024}KB"
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File exceeds the {limit_label} limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)
