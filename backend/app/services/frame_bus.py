import asyncio


class FrameBus:
    """In-memory latest-frame store and pub/sub used to fan out decoded JPEG
    frames to live-view subscribers without each viewer triggering its own
    decode of the camera stream.
    """

    def __init__(self) -> None:
        self._latest: dict[int, bytes] = {}
        self._subscribers: dict[int, set[asyncio.Queue]] = {}

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, camera_id: int, jpeg_bytes: bytes) -> None:
        asyncio.run_coroutine_threadsafe(self.publish(camera_id, jpeg_bytes), loop)

    async def publish(self, camera_id: int, jpeg_bytes: bytes) -> None:
        self._latest[camera_id] = jpeg_bytes
        for queue in list(self._subscribers.get(camera_id, set())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(jpeg_bytes)
            except asyncio.QueueFull:
                pass

    def get_latest(self, camera_id: int) -> bytes | None:
        return self._latest.get(camera_id)

    def subscribe(self, camera_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers.setdefault(camera_id, set()).add(queue)
        return queue

    def unsubscribe(self, camera_id: int, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(camera_id)
        if subs and queue in subs:
            subs.discard(queue)


frame_bus = FrameBus()
