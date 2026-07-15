"""Async API client for LightNVR.

Owns the current access/refresh tokens and hides re-authentication from
every caller (the coordinators, and the camera entity's live-stream proxy):
every read tries the current access token first, refreshes (or, if the
refresh token itself is dead, falls back to a full re-login) on a 401, and
retries once before giving up.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from .const import TOKEN_REFRESH_LEEWAY_SECONDS

_LOGGER = logging.getLogger(__name__)


class LightNVRError(Exception):
    """Base error for this client."""


class LightNVRAuthError(LightNVRError):
    """Both refresh and fallback login failed - a real credential problem
    (wrong/changed password, account deleted). Callers should treat this as
    fatal and trigger Home Assistant's reauth flow.
    """


class LightNVRConnectionError(LightNVRError):
    """Network-level failure: unreachable host or a timeout."""


class LightNVRSSLError(LightNVRConnectionError):
    """The TLS certificate isn't trusted - expected out of the box, since
    LightNVR ships a self-signed cert by default. A subclass of
    LightNVRConnectionError so existing broad `except LightNVRConnectionError`
    callers (the coordinators) still catch it, while config_flow can catch
    this specifically first to show a more actionable error message.
    """


def _decode_jwt_exp(token: str) -> float | None:
    """Read the `exp` claim out of a JWT's payload without verifying its
    signature. This is a local "is it about to expire" check only, on a
    token we already obtained directly from a trusted login/refresh call -
    not a security boundary, so no verification is needed or attempted.
    """
    try:
        payload_b64 = token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return payload.get("exp")
    except Exception:  # noqa: BLE001 - best-effort only, never fatal
        return None


class LightNVRApiClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        username: str,
        password: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = f"https://{host}:{port}/api"
        self._username = username
        self._password = password
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._refresh_lock = asyncio.Lock()
        # Set by __init__.py so a successful login/refresh is immediately
        # persisted into the config entry - refresh rotates the refresh
        # token on every call, so skipping this persist means the next
        # refresh attempt uses an already-invalidated token.
        self.on_tokens_updated: Callable[[str, str], Awaitable[None] | None] | None = None

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def session(self) -> aiohttp.ClientSession:
        """The underlying aiohttp session - exposed for the one case that
        can't go through the request helpers above: the camera entity's
        long-lived MJPEG proxy stream, which needs the raw session to open
        a streaming (not buffered-to-completion) request.
        """
        return self._session

    # ---------------------------------------------------------------- auth

    async def async_login(self) -> None:
        """Full username/password login - used at setup, and as the
        fallback when the refresh token itself is dead."""
        try:
            data = await self._request_json(
                "POST", "/auth/login", authed=False, json={"username": self._username, "password": self._password}
            )
        except aiohttp.ClientResponseError as err:
            # Any non-2xx here (401 wrong password, 429 rate-limited after
            # too many failed attempts, etc.) means the credentials didn't
            # work - wrap it so callers get one well-typed exception rather
            # than having to also know about aiohttp's exception hierarchy.
            raise LightNVRAuthError(f"Login failed: {err}") from err
        await self._store_tokens(data["access_token"], data["refresh_token"])

    async def async_get_me(self) -> dict[str, Any]:
        return await self.async_request_json("GET", "/auth/me")

    async def async_auth_header(self) -> dict[str, str]:
        """Bearer header using a token proactively refreshed if it's close to
        expiring. Used by the camera entity's long-lived MJPEG proxy
        connection, which can't retry mid-stream the way a single
        request/response call can.
        """
        await self._ensure_fresh_token()
        if self._access_token is None:
            raise LightNVRAuthError("Not authenticated")
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _ensure_fresh_token(self) -> None:
        if self._access_token is None:
            await self.async_login()
            return
        exp = _decode_jwt_exp(self._access_token)
        if exp is not None and exp - time.time() < TOKEN_REFRESH_LEEWAY_SECONDS:
            await self._async_refresh_or_relogin()

    # -------------------------------------------------------------- reads

    async def async_get_cameras(self) -> list[dict[str, Any]]:
        return await self.async_request_json("GET", "/cameras")

    async def async_get_motion_status(self) -> dict[str, Any]:
        return await self.async_request_json("GET", "/cameras/motion-status")

    async def async_get_system_status(self) -> dict[str, Any]:
        return await self.async_request_json("GET", "/system/status")

    async def async_get_dashboard(self) -> dict[str, Any]:
        return await self.async_request_json("GET", "/system/dashboard")

    async def async_get_snapshot(self, camera_id: int) -> bytes | None:
        """Latest decoded still. Returns None on the documented 503 ("no
        frame yet") rather than raising, since that's an expected transient
        state right after a camera comes online, not an error. A genuine
        connection failure still propagates as LightNVRConnectionError -
        callers (camera.py's async_camera_image) decide how to degrade that.
        """
        try:
            return await self.async_request_bytes("GET", f"/cameras/{camera_id}/snapshot.jpg")
        except aiohttp.ClientResponseError as err:
            if err.status == 503:
                return None
            raise

    def mjpeg_url(self, camera_id: int, quality: str) -> str:
        return f"{self._base_url}/cameras/{camera_id}/stream.mjpeg?quality={quality}"

    # --------------------------------------------------- request plumbing

    async def async_request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self._with_reauth_retry(self._request_json, method, path, **kwargs)

    async def async_request_bytes(self, method: str, path: str, **kwargs: Any) -> bytes:
        return await self._with_reauth_retry(self._request_bytes, method, path, **kwargs)

    async def _with_reauth_retry(self, fn: Callable[..., Awaitable[Any]], method: str, path: str, **kwargs: Any) -> Any:
        try:
            return await fn(method, path, **kwargs)
        except aiohttp.ClientResponseError as err:
            if err.status != 401:
                raise
            await self._async_refresh_or_relogin()
            return await fn(method, path, **kwargs)

    async def _async_refresh_or_relogin(self) -> None:
        async with self._refresh_lock:
            try:
                await self._async_refresh()
                return
            except LightNVRConnectionError:
                # A genuine network failure isn't a credentials problem -
                # let it propagate as-is rather than also attempting a
                # (equally doomed) fallback login and misreporting this as
                # "reauth needed" when the real issue is "can't reach it".
                raise
            except (LightNVRAuthError, aiohttp.ClientResponseError):
                # Refresh-token lifetime is admin-configurable on the
                # LightNVR side, down to as little as 1 minute - treating
                # every refresh failure as fatal would force spurious reauth
                # prompts on a perfectly healthy install. Only surface
                # LightNVRAuthError (triggering HA's reauth flow) if a full
                # fresh login also fails, i.e. an actual credential problem.
                # async_login() itself always raises LightNVRAuthError (never
                # a raw aiohttp exception) on any failed login, so whatever
                # it raises here is already exactly what the caller expects.
                await self.async_login()

    async def _async_refresh(self) -> None:
        if not self._refresh_token:
            raise LightNVRAuthError("No refresh token available")
        try:
            data = await self._request_json(
                "POST", "/auth/refresh", authed=False, json={"refresh_token": self._refresh_token}
            )
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise LightNVRAuthError("Refresh token rejected") from err
            raise
        await self._store_tokens(data["access_token"], data["refresh_token"])

    async def _store_tokens(self, access_token: str, refresh_token: str) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        if self.on_tokens_updated:
            result = self.on_tokens_updated(access_token, refresh_token)
            if result is not None:
                await result

    async def _request_json(self, method: str, path: str, *, authed: bool = True, **kwargs: Any) -> Any:
        async with self._open_request(method, path, authed=authed, **kwargs) as resp:
            return await resp.json()

    async def _request_bytes(self, method: str, path: str, *, authed: bool = True, **kwargs: Any) -> bytes:
        async with self._open_request(method, path, authed=authed, **kwargs) as resp:
            return await resp.read()

    @asynccontextmanager
    async def _open_request(self, method: str, path: str, *, authed: bool, **kwargs: Any):
        """Issues one HTTP request, mapping connection-level aiohttp
        exceptions (unreachable host, timeout, untrusted TLS cert) to
        LightNVRConnectionError. `raise_for_status()` is called inside the
        `async with` so a non-2xx status raises aiohttp.ClientResponseError,
        which is left to propagate as-is (not wrapped) - the 401 branch of
        that is exactly what `_with_reauth_retry` inspects to trigger a
        refresh-and-retry.
        """
        headers = kwargs.pop("headers", {})
        if authed:
            if self._access_token is None:
                raise LightNVRAuthError("Not authenticated")
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            async with self._session.request(method, f"{self._base_url}{path}", headers=headers, **kwargs) as resp:
                resp.raise_for_status()
                yield resp
        except aiohttp.ClientConnectorCertificateError as err:
            raise LightNVRSSLError(f"TLS certificate not trusted (self-signed?): {err}") from err
        except (aiohttp.ClientConnectionError, TimeoutError, aiohttp.ServerTimeoutError) as err:
            raise LightNVRConnectionError(str(err)) from err
