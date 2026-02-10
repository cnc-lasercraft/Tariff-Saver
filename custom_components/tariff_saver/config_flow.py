"""Config flow for Tariff Saver."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback

from .const import DOMAIN, DEFAULT_PUBLISH_TIME, CONF_PUBLISH_TIME

# Modes
MODE_PUBLIC = "public"
MODE_MYEKZ = "myekz"


class TariffSaverConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tariff Saver."""

    VERSION = 2

    def __init__(self) -> None:
        self._name: str | None = None
        self._mode: str | None = None

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
        """Placeholder for myEKZ OAuth flow."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._name,
                data={
                    CONF_NAME: self._name,
                    "mode": MODE_MYEKZ,
            
                    # ✅ Defaults damit bestehender Coordinator nicht crasht
                    "tariff_name": "myEKZ",
                    "baseline_tariff_name": None,
            
                    CONF_PUBLISH_TIME: user_input.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME),
                },
            )


        schema = vol.Schema({vol.Optional(CONF_PUBLISH_TIME, default=DEFAULT_PUBLISH_TIME): str})
        return self.async_show_form(step_id="myekz", data_schema=schema)

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
