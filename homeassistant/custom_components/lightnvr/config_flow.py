"""Config flow for LightNVR: initial setup, reauth, and options.

Note: step methods deliberately don't type-hint their return value.
Home Assistant's exact result-type alias for config flows (FlowResult vs
ConfigFlowResult) has moved between core versions; omitting the annotation
avoids a hard import that could fail to load entirely on an HA version where
the name differs, at zero runtime cost (Python doesn't require it).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LightNVRApiClient, LightNVRAuthError, LightNVRConnectionError, LightNVRSSLError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_FAST_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SLOW_POLL_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_FAST_POLL_SECONDS,
    DEFAULT_PORT,
    DEFAULT_SLOW_POLL_SECONDS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MIN_FAST_POLL_SECONDS,
    MIN_SLOW_POLL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        # Default OFF: LightNVR ships a self-signed HTTPS cert out of the
        # box, so requiring verification would fail a fresh install by
        # default. Users who've since installed a real/Let's Encrypt cert
        # (Settings -> Security in the app) can turn this on here or later
        # via the options flow.
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)


async def _validate_and_login(hass: Any, data: dict[str, Any]) -> dict[str, str]:
    """Attempt a real login against the given host. Returns the tokens on
    success; raises LightNVRSSLError / LightNVRAuthError / LightNVRConnectionError
    on the ways it can fail, for the caller to map to a form error.
    """
    session = async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL])
    api = LightNVRApiClient(
        session,
        data[CONF_HOST],
        data[CONF_PORT],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )
    await api.async_login()
    await api.async_get_me()  # confirms the token actually works end to end
    return {CONF_ACCESS_TOKEN: api.access_token, CONF_REFRESH_TOKEN: api.refresh_token}


def _map_error(exc: Exception) -> str:
    if isinstance(exc, LightNVRSSLError):
        return "ssl_error"
    if isinstance(exc, LightNVRAuthError):
        return "invalid_auth"
    if isinstance(exc, LightNVRConnectionError):
        return "cannot_connect"
    _LOGGER.exception("Unexpected error validating LightNVR connection")
    return "unknown"


class LightNVRConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                tokens = await _validate_and_login(self.hass, user_input)
            except Exception as err:  # noqa: BLE001 - _map_error dispatches by type
                errors["base"] = _map_error(err)
            else:
                await self.async_set_unique_id(f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"LightNVR ({user_input[CONF_HOST]})",
                    data={**user_input, **tokens},
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                tokens = await _validate_and_login(self.hass, data)
            except Exception as err:  # noqa: BLE001 - _map_error dispatches by type
                errors["base"] = _map_error(err)
            else:
                return self.async_update_reload_and_abort(reauth_entry, data={**data, **tokens})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"host": reauth_entry.data[CONF_HOST]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LightNVROptionsFlow:
        return LightNVROptionsFlow()


class LightNVROptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_FAST_POLL_INTERVAL,
                    default=current.get(CONF_FAST_POLL_INTERVAL, DEFAULT_FAST_POLL_SECONDS),
                ): vol.All(int, vol.Range(min=MIN_FAST_POLL_SECONDS)),
                vol.Required(
                    CONF_SLOW_POLL_INTERVAL,
                    default=current.get(CONF_SLOW_POLL_INTERVAL, DEFAULT_SLOW_POLL_SECONDS),
                ): vol.All(int, vol.Range(min=MIN_SLOW_POLL_SECONDS)),
                vol.Required(
                    CONF_VERIFY_SSL,
                    default=current.get(
                        CONF_VERIFY_SSL, self.config_entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
                    ),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
