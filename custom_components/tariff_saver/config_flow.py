"""Config flow for Tariff Saver (Public + myEKZ OAuth2).

Fix:
- Force OAuth authorize redirect_uri to HA External URL callback:
    <external_url>/auth/external/callback
  This avoids the My Home Assistant proxy redirect:
    https://my.home-assistant.io/redirect/oauth
  which EKZ/Keycloak rejects unless explicitly whitelisted.

How:
- Override AbstractOAuth2FlowHandler.async_get_redirect_uri() in THIS config flow
  (this is the actual flow handler).

Behavior:
- Public mode: creates entry immediately.
- myEKZ mode: asks for redirect_uri (for EKZ linking endpoint) + publish_time,
  generates ems_instance_id, then starts OAuth2 via async_step_pick_implementation().
- After OAuth success: async_step_auth_create_entry creates the entry.

IMPORTANT:
- Settings → System → Network → External URL must be set (Nabu Casa URL).
- Requires oauth2.py + application_credentials.py.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_entry_oauth2_flow

from .const import DOMAIN, DEFAULT_PUBLISH_TIME, CONF_PUBLISH_TIME

_LOGGER = logging.getLogger(__name__)

MODE_PUBLIC = "public"
MODE_MYEKZ = "myekz"


def _generate_ems_instance_id() -> str:
    return f"ha-{uuid.uuid4().hex}"


def _ha_external_oauth_callback(hass) -> str:
    external = hass.config.external_url
    if not external:
        raise HomeAssistantError(
            "External URL is not set. Please set it to your Nabu Casa URL under "
            "Settings → System → Network → External URL."
        )
    return external.rstrip("/") + "/auth/external/callback"


class ConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    DOMAIN = DOMAIN
    VERSION = 2

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    async def async_get_redirect_uri(self) -> str:
        """Return redirect_uri used in the Keycloak authorize URL."""
        return _ha_external_oauth_callback(self.hass)

    def __init__(self) -> None:
        super().__init__()
        self._name: str | None = None
        self._mode: str | None = None
        self._redirect_uri: str | None = None  # EKZ linking return URL (not OAuth)
        self._ems_instance_id: str | None = None
        self._publish_time: str = DEFAULT_PUBLISH_TIME

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            return await self.async_step_mode()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME): str}),
        )

    async def async_step_mode(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._mode = user_input["mode"]
            if self._mode == MODE_PUBLIC:
                return await self.async_step_public()
            return await self.async_step_myekz()

        return self.async_show_form(
            step_id="mode",
            data_schema=vol.Schema(
                {
                    vol.Required("mode", default=MODE_PUBLIC): vol.In(
                        {
                            MODE_PUBLIC: "Public (no login)",
                            MODE_MYEKZ: "myEKZ login",
                        }
                    )
                }
            ),
        )

    async def async_step_public(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(
                title=self._name or "Tariff Saver",
                data={
                    CONF_NAME: self._name or "Tariff Saver",
                    "mode": MODE_PUBLIC,
                    "tariff_name": user_input["tariff_name"],
                    "baseline_tariff_name": user_input.get("baseline_tariff_name"),
                    CONF_PUBLISH_TIME: user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
                },
            )

        return self.async_show_form(
            step_id="public",
            data_schema=vol.Schema(
                {
                    vol.Required("tariff_name"): str,
                    vol.Optional("baseline_tariff_name", default="electricity_standard"): str,
                    vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str,
                }
            ),
        )

    async def async_step_myekz(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            # This redirect_uri is for EKZ /v1/emsLinkStatus linking flow,
            # NOT the OAuth redirect_uri.
            self._redirect_uri = str(user_input["redirect_uri"]).strip()
            self._publish_time = user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)
            self._ems_instance_id = _generate_ems_instance_id()

            # Ensure external URL is set early, to avoid confusing Keycloak errors.
            _ha_external_oauth_callback(self.hass)

            return await self.async_step_pick_implementation()

        default_redirect = (self.hass.config.external_url or "").rstrip("/") + "/"
        return self.async_show_form(
            step_id="myekz",
            data_schema=vol.Schema(
                {
                    vol.Required("redirect_uri", default=default_redirect or "https://"): str,
                    vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str,
                }
            ),
        )

    async def async_step_auth_create_entry(self, data: dict[str, Any]):
        return self.async_create_entry(
            title=self._name or "Tariff Saver",
            data={
                CONF_NAME: self._name or "Tariff Saver",
                "mode": MODE_MYEKZ,
                "ems_instance_id": self._ems_instance_id,
                "redirect_uri": self._redirect_uri,
                "tariff_name": "myEKZ",
                "baseline_tariff_name": None,
                CONF_PUBLISH_TIME: self._publish_time,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        from .options_flow import TariffSaverOptionsFlowHandler

        return TariffSaverOptionsFlowHandler(config_entry)
