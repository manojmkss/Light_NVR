import asyncio
import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import onvif as _onvif_package
from onvif import ONVIFCamera

logger = logging.getLogger(__name__)

# onvif-zeep-async==3.1.12's own default wsdl_dir is wrong - it resolves to
# .../site-packages/wsdl, one directory short of where its WSDL files are
# actually bundled (.../site-packages/onvif/wsdl). Computed from the package's
# own __file__ rather than hardcoded so it keeps working across Python/install
# layout changes instead of silently breaking every ONVIF connection again.
_WSDL_DIR = os.path.join(os.path.dirname(_onvif_package.__file__), "wsdl")


@dataclass
class DiscoveredDevice:
    address: str  # host:port of the ONVIF device service
    host: str
    port: int
    xaddrs: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    # Best-effort, no ONVIF credentials needed - hardware/name come from the
    # standard ONVIF scope strings in the WS-Discovery response itself; MAC
    # comes from the host's ARP table. Both are commonly unavailable: many
    # cameras omit the hardware/name scopes, and MAC lookup only works when
    # the container can see the LAN's real ARP table (host networking) -
    # Docker's default bridge network sits behind NAT and never will.
    hardware_hint: str | None = None
    name_hint: str | None = None
    mac_address: str | None = None


@dataclass
class MediaProfile:
    token: str
    name: str
    stream_uri: str
    width: int | None = None
    height: int | None = None
    # ONVIF VideoSource token: profiles sharing one belong to the same physical
    # camera input. On a standalone camera every profile has the same token; an
    # NVR exposes one per channel - this is what channel grouping keys on.
    source_token: str | None = None


@dataclass
class ChannelInfo:
    """One physical camera input on a multi-channel device (an NVR), with its
    best main/sub stream picked the same way single cameras pick theirs."""
    source_token: str
    label: str
    main_url: str
    sub_url: str | None
    width: int | None = None
    height: int | None = None


@dataclass
class CameraProfileInfo:
    manufacturer: str
    model: str
    firmware_version: str
    profiles: list[MediaProfile]
    recommended_main_token: str | None = None
    recommended_sub_token: str | None = None
    # Populated by the RTSP validation step in _fetch_camera_profiles:
    validated_main_url: str | None = None   # working URL with correct scheme+creds
    validated_sub_url: str | None = None    # working sub-stream URL (may be None)
    resolved_username: str | None = None    # username that worked (None = same as input)
    codec: str | None = None               # h264 | h265 | unknown
    has_audio: bool = False
    # Non-empty only when the device exposes 2+ video sources (it's an NVR):
    # one entry per channel so the frontend can offer "import all channels".
    channels: list[ChannelInfo] = field(default_factory=list)


def _parse_scope_hint(scopes: list[str], category: str) -> str | None:
    prefix = f"onvif://www.onvif.org/{category}/"
    for scope in scopes:
        if scope.startswith(prefix):
            return unquote(scope[len(prefix):]) or None
    return None


def _lookup_mac(ip: str) -> str | None:
    try:
        with open("/proc/net/arp") as f:
            next(f)  # header line
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip:
                    mac = parts[3]
                    if mac and mac != "00:00:00:00:00:00":
                        return mac
    except OSError:
        pass
    return None


def _discover_sync(timeout: float) -> list[DiscoveredDevice]:
    # wsdiscovery is a blocking/synchronous library; run it off the event loop.
    from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery

    wsd = WSDiscovery()
    devices: list[DiscoveredDevice] = []
    wsd.start()
    try:
        services = wsd.searchServices(timeout=timeout)
        for service in services:
            xaddrs = list(service.getXAddrs())
            if not xaddrs:
                continue
            types = [str(t) for t in (service.getTypes() or [])]
            if not any("NetworkVideoTransmitter" in t for t in types):
                # Not every device answers WS-Discovery with a type filter applied,
                # so skip type-based filtering only when no types are advertised at all.
                if types:
                    continue
            parsed = urlparse(xaddrs[0])
            host = parsed.hostname or ""
            port = parsed.port or 80
            if not host:
                continue
            scopes = [str(s) for s in (service.getScopes() or [])]
            devices.append(
                DiscoveredDevice(
                    address=f"{host}:{port}",
                    host=host,
                    port=port,
                    xaddrs=xaddrs,
                    scopes=scopes,
                    hardware_hint=_parse_scope_hint(scopes, "hardware"),
                    name_hint=_parse_scope_hint(scopes, "name"),
                    mac_address=_lookup_mac(host),
                )
            )
    finally:
        wsd.stop()

    # de-duplicate by host
    seen = set()
    unique = []
    for d in devices:
        if d.host in seen:
            continue
        seen.add(d.host)
        unique.append(d)
    return unique


async def discover_onvif_devices(timeout: float = 4.0) -> list[DiscoveredDevice]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _discover_sync, timeout)


# Fallback for when WS-Discovery multicast can't reach the LAN at all - the
# common case on Docker's default bridge network (especially Docker Desktop
# on Windows/Mac): the probe goes out fine, but the camera's unicast reply
# has no matching conntrack entry to route it back into the container, so it
# just gets dropped at the host. Plain unicast TCP/SOAP isn't affected by
# this - NAT handles ordinary request/response traffic normally, which is
# also why the existing "connect directly by IP" field already works.
COMMON_ONVIF_PORTS = [80, 8080, 8000, 2020, 8899, 8081]
_SCAN_CONNECT_TIMEOUT = 2.5
_SCAN_ONVIF_TIMEOUT = 3.0
MAX_SCAN_HOSTS = 3000  # guards against an accidental huge range (e.g. a /8); comfortably covers DEFAULT_SCAN_SUBNETS combined

# Covers the vast majority of home routers out of the box - common ISP/retail
# defaults (192.168.1.0/24, 192.168.0.0/24), JioFiber (192.168.29.0/24) and
# Xiaomi/Mi routers (192.168.31.0/24, both very common in India alongside the
# generic defaults), plus a handful of other widely-seen vendor defaults.
# Scanned automatically as a fallback once multicast comes back empty, so a
# typical home network gets found with zero configuration; a network on an
# uncommon subnet still needs the manual "scan a range" field once, after
# which it can be saved (DiscoverySettings.custom_subnets) to also be covered
# by this automatic pass from then on.
# Ordered most-to-least likely so a typical home network is found in the
# first pass or two rather than waiting through the full list - each /24
# pass takes ~40s (see _SCAN_CONNECT_TIMEOUT), so kept to 6 rather than
# covering every conceivable vendor default.
DEFAULT_SCAN_SUBNETS = [
    "192.168.1.0/24",
    "192.168.0.0/24",
    "192.168.29.0/24",
    "192.168.31.0/24",
    "192.168.8.0/24",
    "192.168.100.0/24",
]


async def _tcp_probe(host: str, port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=_SCAN_CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def _confirm_onvif(host: str, port: int) -> DiscoveredDevice | None:
    try:
        camera = ONVIFCamera(host, port, "", "", wsdl_dir=_WSDL_DIR)
        await asyncio.wait_for(camera.update_xaddrs(), timeout=_SCAN_ONVIF_TIMEOUT)
    except Exception:
        return None  # didn't answer like an ONVIF device at all

    hardware_hint = name_hint = None
    try:
        device_service = await camera.create_devicemgmt_service()
        info = await asyncio.wait_for(device_service.GetDeviceInformation(), timeout=_SCAN_ONVIF_TIMEOUT)
        hardware_hint = getattr(info, "Model", None)
        name_hint = getattr(info, "Manufacturer", None)
    except Exception:
        pass  # most cameras require credentials for this - still confirmed ONVIF without it

    return DiscoveredDevice(
        address=f"{host}:{port}",
        host=host,
        port=port,
        hardware_hint=hardware_hint,
        name_hint=name_hint,
        mac_address=_lookup_mac(host),
    )


async def find_onvif_port(host: str, preferred_port: int | None = None) -> int:
    """Try common ONVIF ports on a single host and return the first that
    actually speaks ONVIF, checking the preferred port first.

    TCP-only probing is intentionally avoided here: a host may have a
    non-ONVIF service (e.g. a web UI that returns HTTP 500 for SOAP calls)
    on an earlier port in the list, which would cause a probe against the
    wrong port.  This function therefore does a full ONVIF handshake
    (_confirm_onvif) on each TCP-reachable port before declaring it the
    winner.  The extra latency (a few seconds per open port) is acceptable
    for a single-host lookup triggered by the user clicking "Connect".
    """
    ports: list[int] = []
    if preferred_port is not None:
        ports.append(preferred_port)
    ports.extend(p for p in COMMON_ONVIF_PORTS if p != preferred_port)

    for port in ports:
        # Fast TCP gate — skip definitely-closed ports without ONVIF overhead.
        if not await _tcp_probe(host, port):
            continue
        # Verify that the open port actually speaks ONVIF before committing.
        # A non-ONVIF service (404, 500, etc.) on this port must not be
        # chosen over a real ONVIF service on a later port in the list.
        if await _confirm_onvif(host, port) is not None:
            return port

    tried = ", ".join(str(p) for p in ports)
    raise ConnectionError(f"No ONVIF device reachable on {host} - tried ports {tried}")


async def scan_ip_range(cidr: str) -> list[DiscoveredDevice]:
    hosts = _expand_cidr(cidr)
    if len(hosts) > MAX_SCAN_HOSTS:
        raise ValueError(f"Range too large ({len(hosts)} addresses) - use a /22 or smaller")
    return await _scan_hosts(hosts)


def _expand_cidr(cidr: str) -> list[str]:
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        raise ValueError(f"Invalid network '{cidr}': {exc}") from exc
    return [str(ip) for ip in network.hosts()]


async def scan_subnets(cidrs: list[str]) -> list[DiscoveredDevice]:
    """Scans each subnet one at a time at the same scale already proven
    reliable for a single manual range scan, stopping as soon as something
    is found - covers the common "which of these common subnets is my LAN"
    case quickly without combining everything into one giant scan. A single
    oversized combined scan (~2500 hosts across every default subnet) was
    tried first and measured to intermittently miss a real, responsive
    device even with bounded concurrency - apparently sustained high
    connection churn over the ~100s+ runtime matters, not just peak
    concurrency. Scanning per-subnet avoids that scale entirely.
    """
    for cidr in cidrs:
        devices = await scan_ip_range(cidr)
        if devices:
            return devices
    return []


# Bounds the actual number of in-flight TCP connection attempts, not just how
# many hosts are "in progress" - a host-level-only semaphore still let each
# host fan out all of COMMON_ONVIF_PORTS unbounded, so a full /22 scan
# briefly opened ~100 hosts x 6 ports = ~600 concurrent connections. That's
# enough event-loop scheduling pressure that even a real, fast LAN response
# can miss the per-probe timeout - confirmed by re-running the exact same
# scan against a known-good device and seeing it intermittently vanish under
# full-range load but never under a small range. A separate, smaller
# semaphore for the heavier ONVIF SOAP handshake keeps that stage from
# suffering the same problem once a batch of ports comes back open.
async def _scan_hosts(hosts: list[str]) -> list[DiscoveredDevice]:
    tcp_semaphore = asyncio.Semaphore(100)
    onvif_semaphore = asyncio.Semaphore(20)

    async def bounded_tcp_probe(ip: str, port: int) -> bool:
        async with tcp_semaphore:
            return await _tcp_probe(ip, port)

    async def bounded_confirm(ip: str, port: int) -> DiscoveredDevice | None:
        async with onvif_semaphore:
            return await _confirm_onvif(ip, port)

    async def check_host(ip: str) -> DiscoveredDevice | None:
        open_flags = await asyncio.gather(*(bounded_tcp_probe(ip, port) for port in COMMON_ONVIF_PORTS))
        for port, is_open in zip(COMMON_ONVIF_PORTS, open_flags):
            if not is_open:
                continue
            device = await bounded_confirm(ip, port)
            if device:
                return device
        return None

    results = await asyncio.gather(*(check_host(ip) for ip in hosts))

    seen = set()
    unique = []
    for device in results:
        if device is None or device.host in seen:
            continue
        seen.add(device.host)
        unique.append(device)
    return unique


def _pick_main_and_sub(profiles: list[MediaProfile]) -> tuple[str | None, str | None]:
    """Highest resolution profile becomes the recording (main) stream, lowest
    becomes the live-view/motion (sub) stream - this is what lets "one-click
    add" skip making the user pick streams manually. Falls back to the first
    profile for both if no profile reports a resolution.
    """
    with_resolution = [p for p in profiles if p.width and p.height]
    if not with_resolution:
        token = profiles[0].token if profiles else None
        return token, token

    by_area = sorted(with_resolution, key=lambda p: p.width * p.height)
    return by_area[-1].token, by_area[0].token


def _channel_number_from_uri(uri: str) -> int | None:
    """Best-effort channel number from the vendor URL shapes seen in the wild:
    Dahua/CP-Plus style `?channel=N`, Hikvision style `/Streaming/Channels/N01`.
    Returns None when the URL doesn't say - the caller falls back to ordinals.
    """
    m = re.search(r"[?&]channel=(\d+)", uri)
    if m:
        return int(m.group(1))
    m = re.search(r"/Streaming/Channels/(\d+)\d{2}(?:\D|$)", uri)
    if m:
        return int(m.group(1))
    return None


def _group_channels(profiles: list[MediaProfile]) -> list[ChannelInfo]:
    """Group profiles by ONVIF video source. Two or more sources means the
    device is an NVR/multi-channel unit; each group gets the same
    highest-res-main / lowest-res-sub treatment a standalone camera gets.
    Returns [] for single-source devices so the ordinary flow is untouched.
    """
    groups: dict[str, list[MediaProfile]] = {}
    for p in profiles:
        if p.source_token:
            groups.setdefault(p.source_token, []).append(p)
    if len(groups) < 2:
        return []

    entries = []
    for token, members in groups.items():
        main_token, sub_token = _pick_main_and_sub(members)
        main_p = next((m for m in members if m.token == main_token), members[0])
        sub_p = None
        if sub_token and sub_token != main_p.token:
            sub_p = next((m for m in members if m.token == sub_token), None)
        entries.append((_channel_number_from_uri(main_p.stream_uri), token, main_p, sub_p))

    # Order by the channel number the URL claims when present, else keep the
    # device's own ordering; label with the same number so what the user sees
    # matches the NVR's own UI.
    entries.sort(key=lambda e: (e[0] is None, e[0] or 0))
    channels: list[ChannelInfo] = []
    for i, (num, token, main_p, sub_p) in enumerate(entries, start=1):
        channels.append(
            ChannelInfo(
                source_token=token,
                label=f"Channel {num if num is not None else i}",
                main_url=main_p.stream_uri,
                sub_url=sub_p.stream_uri if sub_p else None,
                width=main_p.width,
                height=main_p.height,
            )
        )
    return channels


# Outer budget for the whole probe: ONVIF handshake (~10s) + RTSP validation
# (2 schemes x 2 usernames x ~10s worst case = 40s) + sub-stream detection
# (max ~16s). Keep this above the sum of the per-attempt timeouts below or
# slow cameras get cut off mid-fallback with a confusing timeout error.
PROBE_TIMEOUT_SECONDS = 75


async def fetch_camera_profiles(host: str, port: int, username: str, password: str) -> CameraProfileInfo:
    try:
        return await asyncio.wait_for(
            _fetch_camera_profiles(host, port, username, password), timeout=PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        raise ConnectionError(f"Timed out after {PROBE_TIMEOUT_SECONDS}s connecting to {host}:{port}") from exc


async def _fetch_camera_profiles(host: str, port: int, username: str, password: str) -> CameraProfileInfo:
    from app.services.stream_probe import (
        candidate_sub_urls,
        inject_rtsp_credentials,
        probe_rtsp_stream,
        probe_rtsp_with_fallbacks,
        strip_onvif_params,
    )

    camera = ONVIFCamera(host, port, username, password, wsdl_dir=_WSDL_DIR)
    await camera.update_xaddrs()

    device_service = await camera.create_devicemgmt_service()
    info = await device_service.GetDeviceInformation()

    media_service = await camera.create_media_service()
    onvif_profiles = await media_service.GetProfiles()

    profiles: list[MediaProfile] = []
    for profile in onvif_profiles:
        request = media_service.create_type("GetStreamUri")
        request.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
        request.ProfileToken = profile.token
        uri_response = await media_service.GetStreamUri(request)

        width = height = None
        resolution = getattr(getattr(profile, "VideoEncoderConfiguration", None), "Resolution", None)
        if resolution is not None:
            width = getattr(resolution, "Width", None)
            height = getattr(resolution, "Height", None)

        profiles.append(
            MediaProfile(
                token=profile.token,
                name=profile.Name,
                stream_uri=strip_onvif_params(uri_response.Uri),
                width=width,
                height=height,
                source_token=getattr(
                    getattr(profile, "VideoSourceConfiguration", None), "SourceToken", None
                ),
            )
        )

    # Multi-channel detection: 2+ distinct video sources = an NVR. Picking
    # main/sub across ALL profiles would pair one channel's main stream with a
    # DIFFERENT channel's sub stream, so recommend within the first source only.
    source_tokens = [p.source_token for p in profiles if p.source_token]
    is_multichannel = len(set(source_tokens)) >= 2
    if is_multichannel:
        first_source = source_tokens[0]
        main_token, sub_token = _pick_main_and_sub([p for p in profiles if p.source_token == first_source])
    else:
        main_token, sub_token = _pick_main_and_sub(profiles)

    # ── RTSP validation: find scheme (rtsp/rtsps), working credentials, codec ──
    validated_main_url: str | None = None
    validated_sub_url: str | None = None
    resolved_username: str | None = None
    codec: str | None = None
    has_audio = False

    main_profile = next((p for p in profiles if p.token == main_token), None)
    if main_profile:
        raw_main = inject_rtsp_credentials(main_profile.stream_uri, username, password)
        try:
            working_main, stream_info = await probe_rtsp_with_fallbacks(
                raw_main, username, password, timeout=5.0
            )
            validated_main_url = working_main
            codec = stream_info.codec
            has_audio = stream_info.has_audio

            # Determine the username that actually worked (may differ from input)
            parsed_working = urlparse(working_main)
            if parsed_working.username and parsed_working.username != username:
                resolved_username = parsed_working.username

            effective_user = resolved_username or username

            # Apply the working scheme/credentials to all profiles so the
            # form shows corrected URIs for any profile the user might pick.
            use_rtsps = working_main.startswith("rtsps://")
            for p in profiles:
                uri = inject_rtsp_credentials(p.stream_uri, effective_user, password)
                if use_rtsps and uri.startswith("rtsp://"):
                    uri = "rtsps://" + uri[7:]
                p.stream_uri = uri

            # ── Sub-stream detection ──
            sub_profile = next((p for p in profiles if p.token == sub_token), None)
            if sub_profile and sub_token != main_token:
                # ONVIF returned a distinct sub profile - validate it
                try:
                    await probe_rtsp_stream(sub_profile.stream_uri, timeout=4.0)
                    validated_sub_url = sub_profile.stream_uri  # already has creds from above loop
                except ConnectionError:
                    validated_sub_url = sub_profile.stream_uri  # use it anyway; ONVIF said it exists
            else:
                # No distinct ONVIF sub profile - try the two most likely URL
                # mutations only, so this step stays inside the probe budget.
                for candidate in candidate_sub_urls(working_main)[:2]:
                    try:
                        await probe_rtsp_stream(candidate, timeout=3.0)
                        validated_sub_url = candidate
                        break
                    except ConnectionError:
                        pass

        except ConnectionError as exc:
            logger.warning("RTSP validation failed for %s: %s", host, exc)

    # Built AFTER validation on purpose: the fixup loop above rewrote every
    # profile's stream_uri with the working scheme + credentials, so channel
    # URLs inherit them. (When validation failed entirely, the probe route
    # injects the caller's credentials as a fallback, same as for profiles.)
    channels = _group_channels(profiles) if is_multichannel else []

    return CameraProfileInfo(
        manufacturer=getattr(info, "Manufacturer", "unknown"),
        model=getattr(info, "Model", "unknown"),
        firmware_version=getattr(info, "FirmwareVersion", "unknown"),
        profiles=profiles,
        recommended_main_token=main_token,
        recommended_sub_token=sub_token,
        validated_main_url=validated_main_url,
        validated_sub_url=validated_sub_url,
        resolved_username=resolved_username,
        codec=codec,
        has_audio=has_audio,
        channels=channels,
    )
