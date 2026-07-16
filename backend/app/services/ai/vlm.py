"""Tier 3: turning a detection snapshot into one human sentence.

Three providers, one function. "ollama" gets first-class treatment (its own
native API, no key, model listing) because a box on the same LAN running
Ollama is the sweet spot for this feature: descriptions without any footage
leaving the house and without per-call cost. "openai_compatible" covers
LM Studio / vLLM / OpenAI itself; "anthropic" covers Claude directly.
"""

import base64
import logging

import httpx

logger = logging.getLogger(__name__)

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


async def describe_frame(settings, jpeg: bytes, labels: list[str], camera_name: str) -> str:
    """One sentence for one frame, via whichever provider is configured.
    Raises on misconfiguration/unreachability - callers decide whether that's
    a logged warning (pipeline) or a surfaced error (the Test button)."""
    prompt = _PROMPT.format(camera=camera_name, labels=", ".join(labels) or "none")
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
