"""Tier 3: turning a detection snapshot into one human sentence.

Three providers, one function. "ollama" gets first-class treatment (its own
native API, no key, model listing) because a box on the same LAN running
Ollama is the sweet spot for this feature: descriptions without any footage
leaving the house and without per-call cost. "openai_compatible" covers
LM Studio / vLLM / OpenAI itself; "anthropic" covers Claude directly.
"""

import asyncio
import base64
import logging

import httpx

logger = logging.getLogger(__name__)

# The describer on this NVR (docker-compose.describe.yml): Ollama on the same
# machine, usually on its CPU. It gets one picture at a time, smaller
# pictures, more time, and only the summaries that are actually sent.
LOCAL_DESCRIBER_URL = "http://ai-describe:11434"
LOCAL_TIMEOUT_SECONDS = 180.0
LOCAL_MAX_SIDE = 768  # less to send; reads a scene fine (the model resizes to its own grid anyway)
LOCAL_MAX_WAITING = 2  # more than this queued and it has fallen behind: skip, don't pile up
_local_gate = asyncio.Semaphore(1)
_local_waiting = 0


class DescriberBusy(RuntimeError):
    """The local describer is still working through earlier pictures."""


def is_local(settings) -> bool:
    return (getattr(settings, "vlm_provider", "") == "ollama"
            and (getattr(settings, "vlm_url", "") or "").rstrip("/") == LOCAL_DESCRIBER_URL)


def timeout_for(settings) -> float:
    """How long a caller should wait for one description."""
    return LOCAL_TIMEOUT_SECONDS if is_local(settings) else 30.0


def _shrink(jpeg: bytes, max_side: int) -> bytes:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None or max(img.shape[:2]) <= max_side:
        return jpeg
    scale = max_side / max(img.shape[:2])
    img = cv2.resize(img, (round(img.shape[1] * scale), round(img.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    return buf.tobytes() if ok else jpeg

# Local VLMs can take tens of seconds on their first call while the model
# loads into RAM/VRAM; a short timeout would make the feature look broken
# exactly once per boot. Descriptions are generated in the background, so a
# generous timeout costs nothing in alert latency.
VLM_TIMEOUT_SECONDS = 60.0

_PROMPT = (
    "You are writing a one-line alert for the home security camera '{camera}'. "
    "Objects detected: {labels}. Describe what is happening in the image in one "
    "factual sentence of at most 25 words. No preamble, no speculation beyond the image."
)


def openai_endpoint(url: str) -> str:
    """Normalise whatever base URL the user pasted into a chat-completions
    endpoint. People paste all three shapes; making them all work beats
    documenting which one we meant."""
    url = url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


async def list_ollama_models(url: str) -> list[str]:
    """What's installed on an Ollama host - drives the model dropdown in
    Settings so users pick from reality instead of typing from memory."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(f"{url.rstrip('/')}/api/tags")
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]


async def describe_frame(
    settings, jpeg: bytes, labels: list[str], camera_name: str, prompt: str | None = None
) -> str:
    """One sentence for one frame, via whichever provider is configured.
    Raises on misconfiguration/unreachability - callers decide whether that's
    a logged warning (pipeline) or a surfaced error (the Test button).
    `prompt` overrides the default one-line-alert prompt (event summaries)."""
    prompt = prompt or _PROMPT.format(camera=camera_name, labels=", ".join(labels) or "none")
    if is_local(settings):
        return await _describe_local(settings, jpeg, prompt)
    b64 = base64.b64encode(jpeg).decode()

    provider = settings.vlm_provider
    if provider == "ollama":
        if not settings.vlm_url:
            raise ValueError("Ollama URL is not set")
        if not settings.vlm_model:
            raise ValueError("No Ollama model selected (e.g. llama3.2-vision, llava, minicpm-v)")
        async with httpx.AsyncClient(timeout=VLM_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{settings.vlm_url.rstrip('/')}/api/chat",
                json={
                    "model": settings.vlm_model,
                    "stream": False,
                    "options": {"temperature": 0.2},
                    "messages": [{"role": "user", "content": prompt, "images": [b64]}],
                },
            )
            resp.raise_for_status()
            text = resp.json().get("message", {}).get("content", "")

    elif provider == "anthropic":
        if not settings.vlm_api_key:
            raise ValueError("Anthropic API key is not set")
        if not settings.vlm_model:
            raise ValueError("No Anthropic model set (e.g. claude-haiku-4-5-20251001)")
        base = settings.vlm_url.rstrip("/") if settings.vlm_url else "https://api.anthropic.com"
        async with httpx.AsyncClient(timeout=VLM_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}/v1/messages",
                headers={
                    "x-api-key": settings.vlm_api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": settings.vlm_model,
                    "max_tokens": 100,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                },
            )
            resp.raise_for_status()
            blocks = resp.json().get("content", [])
            text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    else:  # openai_compatible: LM Studio, vLLM, OpenAI, many others
        if not settings.vlm_url:
            raise ValueError("Endpoint URL is not set")
        if not settings.vlm_model:
            raise ValueError("No model name set")
        headers = {}
        if settings.vlm_api_key:
            headers["Authorization"] = f"Bearer {settings.vlm_api_key}"
        async with httpx.AsyncClient(timeout=VLM_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                openai_endpoint(settings.vlm_url),
                headers=headers,
                json={
                    "model": settings.vlm_model,
                    "max_tokens": 100,
                    "temperature": 0.2,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            ],
                        }
                    ],
                },
            )
            resp.raise_for_status()
            choices = resp.json().get("choices", [])
            text = choices[0]["message"]["content"] if choices else ""

    # One line, bounded: this lands in alert messages and DB rows, and a
    # rambling model must not turn either into an essay.
    text = " ".join(text.strip().split())
    return text[:300]


async def _describe_local(settings, jpeg: bytes, prompt: str) -> str:
    """This NVR's own describer: one at a time, and skipped rather than
    queued without end when it falls behind."""
    global _local_waiting
    if not settings.vlm_model:
        raise ValueError("No description model set up yet (Settings -> AI -> Describe what's happening)")
    if _local_waiting >= LOCAL_MAX_WAITING:
        raise DescriberBusy("the describer on this NVR is still busy with earlier visits - skipped this one")
    _local_waiting += 1
    try:
        async with _local_gate:
            jpeg = await asyncio.to_thread(_shrink, jpeg, LOCAL_MAX_SIDE)
            async with httpx.AsyncClient(timeout=LOCAL_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{LOCAL_DESCRIBER_URL}/api/chat",
                    json={
                        "model": settings.vlm_model,
                        "stream": False,
                        "think": False,  # a thinking pass would triple the time on a CPU
                        "keep_alive": "5m",
                        # 4096 holds a picture (~1100 tokens) and the answer; the
                        # model's own 256K default would reserve gigabytes.
                        "options": {"temperature": 0.2, "num_predict": 90, "num_ctx": 4096},
                        "messages": [{"role": "user", "content": prompt,
                                      "images": [base64.b64encode(jpeg).decode()]}],
                    },
                )
                resp.raise_for_status()
                return " ".join(resp.json().get("message", {}).get("content", "").split()).strip()
    finally:
        _local_waiting -= 1
