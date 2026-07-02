import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlparse


@dataclass
class StreamInfo:
    codec: str  # h264 | h265 | unknown
    has_audio: bool
    width: int | None
    height: int | None
    fps: float | None


# ONVIF GetStreamUri appends these to the URI — they are ONVIF interop params,
# not standard RTSP params, and confuse FFmpeg/OpenCV with some NVRs.
_ONVIF_STRIP_PARAMS = {"unicast", "proto"}


def strip_onvif_params(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    filtered = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
                if k.lower() not in _ONVIF_STRIP_PARAMS}
    return parsed._replace(query=urlencode(filtered) if filtered else "").geturl()


def inject_rtsp_credentials(url: str, username: str, password: str) -> str:
    """Inject username:password into an RTSP/RTSPS URL, replacing any existing
    credentials. Percent-encodes both so a password containing @ : / # etc.
    doesn't corrupt the URL. Returns the URL unchanged when username is blank.
    """
    if not username:
        return url
    parsed = urlparse(url)
    netloc = f"{quote(username, safe='')}:{quote(password or '', safe='')}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _alternate_scheme(url: str) -> str:
    """Swap rtsp:// ↔ rtsps://."""
    if url.startswith("rtsp://"):
        return "rtsps://" + url[7:]
    if url.startswith("rtsps://"):
        return "rtsp://" + url[8:]
    return url


async def probe_rtsp_stream(rtsp_url: str, timeout: float = 8.0) -> StreamInfo:
    """Run ffprobe against an RTSP URL to detect codec/audio/resolution.

    Uses TCP transport since UDP is frequently blocked/lossy through NAT or
    Docker bridge networks, which makes ffprobe hang or fail intermittently.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-timeout", str(int(timeout * 1_000_000)),
        "-print_format", "json",
        "-show_streams",
        rtsp_url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ConnectionError(f"Timed out probing {rtsp_url}") from exc

    if proc.returncode != 0:
        raise ConnectionError(f"ffprobe failed: {stderr.decode(errors='ignore')[:300]}")

    data = json.loads(stdout.decode(errors="ignore"))
    streams = data.get("streams", [])

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise ConnectionError("No video stream found")

    codec_name = video.get("codec_name", "").lower()
    if codec_name in ("h264", "avc"):
        codec = "h264"
    elif codec_name in ("h265", "hevc"):
        codec = "h265"
    else:
        codec = "unknown"

    fps = None
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate")
    if rate and rate != "0/0":
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / float(den) if den else float(num)
        except (ValueError, ZeroDivisionError):
            fps = None

    return StreamInfo(
        codec=codec,
        has_audio=audio is not None,
        width=video.get("width"),
        height=video.get("height"),
        fps=fps,
    )


async def probe_rtsp_with_fallbacks(
    url: str,
    username: str,
    password: str,
    timeout: float = 6.0,
) -> tuple[str, StreamInfo]:
    """Connect to an RTSP stream, automatically correcting common issues:

    - Strips ONVIF-only URL params (unicast=true, proto=Onvif) that confuse clients
    - Tries rtsp:// first, then rtsps:// (RTSP-over-TLS) if the server closes
      the connection immediately (common on NVRs whose ONVIF is HTTPS-only)
    - Tries the given username first, then falls back to 'admin' on
      401 Unauthorized (some NVRs use separate ONVIF and RTSP account namespaces)

    Returns (working_url_with_embedded_credentials, StreamInfo).
    Raises ConnectionError listing all combinations tried if none work.
    """
    base = strip_onvif_params(url)
    alt = _alternate_scheme(base)
    schemes = [base] if base == alt else [base, alt]

    # Try the supplied username first, then 'admin' as the single fallback -
    # many NVRs keep separate ONVIF and RTSP account namespaces where the RTSP
    # side only knows 'admin'. Wider guessing (root, administrator, ...) was
    # dropped deliberately: hammering extra accounts trips brute-force
    # lockouts on some cameras and blows the probe's time budget.
    usernames = [username]
    if username.lower() != "admin":
        usernames.append("admin")

    last_err = "no attempts made"
    tried: list[str] = []

    for scheme_url in schemes:
        for uname in usernames:
            test_url = inject_rtsp_credentials(scheme_url, uname, password)
            tried.append(f"{scheme_url[:8]}… user={uname}")
            try:
                info = await probe_rtsp_stream(test_url, timeout=timeout)
                return test_url, info
            except ConnectionError as exc:
                msg = str(exc)
                last_err = msg
                is_auth_err = "401" in msg or "nauthorized" in msg or "403" in msg or "forbidden" in msg.lower()
                if is_auth_err:
                    # Wrong password or username — try next username with same scheme
                    continue
                else:
                    # Not an auth error (EOF/TLS mismatch, timeout, bad data).
                    # Remaining usernames won't help; skip to the other scheme.
                    break

    raise ConnectionError(
        f"No working RTSP connection found after {len(tried)} attempt(s) "
        f"(tried: {'; '.join(tried)}). Last error: {last_err}"
    )


def candidate_sub_urls(main_url: str) -> list[str]:
    """Generate plausible sub-stream URL candidates from a working main stream URL.
    The URL must already have credentials embedded.
    """
    parsed = urlparse(main_url)
    candidates: list[str] = []

    # 1. subtype=0 → subtype=1  (Dahua / CP-Plus NVR style)
    if parsed.query:
        params = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        if params.get("subtype") == "0":
            new_params = {**params, "subtype": "1"}
            candidates.append(parsed._replace(query=urlencode(new_params)).geturl())

    # 2. /Streaming/Channels/N01 → /Streaming/Channels/N02  (Hikvision-compatible)
    m = re.search(r"(/Streaming/Channels/)(\d+)(01)(/|$)", parsed.path)
    if m:
        new_path = parsed.path[: m.start(3)] + "02" + parsed.path[m.end(3):]
        candidates.append(parsed._replace(path=new_path).geturl())

    # 3. /ch01/main → /ch01/sub  (Reolink and some others)
    if "/main" in parsed.path:
        candidates.append(parsed._replace(path=parsed.path.replace("/main", "/sub", 1)).geturl())

    # 4. /live/channel0 → /live/channel1  (CP-Plus Wi-Fi)
    m2 = re.search(r"(channel)(\d+)(/|$)", parsed.path)
    if m2 and m2.group(2) == "0":
        new_path = parsed.path[: m2.start(2)] + "1" + parsed.path[m2.end(2):]
        candidates.append(parsed._replace(path=new_path).geturl())

    return candidates
