"""Config flow for Tariff Saver (Public + myEKZ OAuth2).

Public mode:
- As before: user enters tariff_name etc.

myEKZ mode:
- User enters redirect_uri (required by EKZ)
- ems_instance_id is auto-generated once
- OAuth2 login is started using HA standard OAuth2 flow
- Only after OAuth succeeds, the config entry is created
  -> this ensures entry.data['auth_implementation'] exists

IMPORTANT:
- No entity renames.
- Public mode unchanged.
"""
from __future__ import annotations

import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow

from .const import DOMAIN, DEFAULT_PUBLISH_TIME, CONF_PUBLISH_TIME

# Modes
MODE_PUBLIC = "public"
MODE_MYEKZ = "myekz"


def _generate_ems_instance_id() -> str:
    """Generate a unique, persistent EMS instance id."""
    return f"ha-{uuid.uuid4().hex}"


class TariffSaverConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    VERSION = 2

    def __init__(self) -> None:
        self._name: str | None = None
        self._mode: str | None = None
        self._redirect_uri: str | None = None
        self._ems_instance_id: str | None = None
        self._publish_time: str = DEFAULT_PUBLISH_TIME

    async def async_step_user(self, user_input=None):
        """Initial step: choose integration name."""
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            return await self.async_step_mode()

        schema = vol.Schema({vol.Required(CONF_NAME): str})
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_mode(self, user_input=None):
        """Choose authentication mode."""
        if user_input is not None:
            self._mode = user_input["mode"]
            if self._mode == MODE_PUBLIC:
                return await self.async_step_public()
            return await self.async_step_myekz()

        schema = vol.Schema(
            {
                vol.Required("mode", default=MODE_PUBLIC): vol.In(
                    {
                        MODE_PUBLIC: "Public (no login)",
                        MODE_MYEKZ: "myEKZ login",
                    }
                )
            }
        )
        return self.async_show_form(step_id="mode", data_schema=schema)

    async def async_step_public(self, user_input=None):
        """Public (no-login) configuration."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_NAME: self._name,
                    "mode": MODE_PUBLIC,
                    "tariff_name": user_input["tariff_name"],
                    "baseline_tariff_name": user_input.get("baseline_tariff_name"),
                    CONF_PUBLISH_TIME: user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
                },
            )

        schema = vol.Schema(
            {
                vol.Required("tariff_name"): str,
                vol.Optional("baseline_tariff_name", default="electricity_standard"): str,
                vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str,  # HH:MM
            }
        )
        return self.async_show_form(step_id="public", data_schema=schema)

    async def async_step_myekz(self, user_input=None):
        """Collect non-secret myEKZ parameters, then start OAuth2."""
        if user_input is not None:
            self._redirect_uri = user_input["redirect_uri"].strip()
            self._publish_time = user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)

            # Generate once for this setup
            self._ems_instance_id = _generate_ems_instance_id()

            # Now start OAuth2 flow (this sets auth_implementation on success)
            return await self.async_step_oauth()

        default_redirect = (self.hass.config.external_url or "").rstrip("/") + "/"
        schema = vol.Schema(
            {
                vol.Required("redirect_uri", default=default_redirect or "https://"): str,
                vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str,
            }
        )
        return self.async_show_form(step_id="myekz", data_schema=schema)

    async def async_step_oauth(self, user_input=None):
        """Start OAuth2 flow (redirect to myEKZ Keycloak)."""
        return await super().async_step_oauth(user_input)

    async def async_step_oauth_create_entry(self, data: dict):
        """Create the config entry after OAuth2 success."""
        # data contains the oauth token etc and ensures auth_implementation is set.

        return self.async_create_entry(
            title=self._name or "Tariff Saver",
            data={
                CONF_NAME: self._name or "Tariff Saver",
                "mode": MODE_MYEKZ,

                # required by EKZ protected endpoints
                "ems_instance_id": self._ems_instance_id,
                "redirect_uri": self._redirect_uri,

                # placeholders to keep existing coordinator logic stable
                "tariff_name": "myEKZ",
                "baseline_tariff_name": None,

                CONF_PUBLISH_TIME: self._publish_time,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler.

        IMPORTANT:
        Options flow lives in options_flow.py. Do NOT define it in this file,
        otherwise changes to options_flow.py will never be used.
        """
        from .options_flow import TariffSaverOptionsFlowHandler

        return TariffSaverOptionsFlowHandler(config_entry)
