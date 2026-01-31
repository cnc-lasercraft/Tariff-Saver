"""Config flow for Tariff Saver.

Two setup paths:
- Public API (manual tariff_name, validated)
- OAuth2 (myEKZ) if application credentials are available
"""
from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import EkzTariffApi
from .const import DOMAIN as TS_DOMAIN

_LOGGER = logging.getLogger(__name__)

SETUP_MODE = "setup_mode"
MODE_OAUTH = "oauth"
MODE_PUBLIC = "public"


class TariffSaverConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=TS_DOMAIN):
    """Handle a config flow for Tariff Saver."""

    DOMAIN = TS_DOMAIN
    VERSION = 5

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    async def async_step_user(self, user_input=None):
        """Choose setup mode."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required(SETUP_MODE, default=MODE_PUBLIC): vol.In(
                        {
                            MODE_PUBLIC: "Public API (manual tariff, validated)",
                            MODE_OAUTH: "myEKZ Login (OAuth2)",
                        }
                    )
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema)

        mode = user_input[SETUP_MODE]
        if mode == MODE_OAUTH:
            return await self.async_step_oauth_start()

        return await self.async_step_public()

    async def async_step_public(self, user_input=None):
        """Public API setup: ask for tariff_name and validate it."""
        errors: dict[str, str] = {}

        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required("tariff_name"): str,
                    vol.Optional("baseline_tariff_name", default="electricity_standard"): str,
                    vol.Optional("publish_time", default=DEFAULT_PUBLISH_TIME): str,  # "HH:MM"
                }
            )

            return self.async_show_form(step_id="public", data_schema=schema, errors=errors)
            
        tariff_name = user_input["tariff_name"].strip()
        baseline_tariff_name = user_input.get("baseline_tariff_name", "electricity_standard").strip()


        # Validate by fetching a short time range (today)
        session = async_get_clientsession(self.hass)
        api = EkzTariffApi(session)
        now = dt_util.utcnow()
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=6)

        try:
            items = await api.fetch_prices(tariff_name=tariff_name, start=start, end=end)
            if not items:
                errors["base"] = "no_data"
        except aiohttp.ClientResponseError:
            errors["base"] = "cannot_connect"
        except aiohttp.ClientError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            errors["base"] = "unknown"

        if errors:
            schema = vol.Schema({vol.Required("tariff_name", default=tariff_name): str})
            return self.async_show_form(step_id="public", data_schema=schema, errors=errors)

        return self.async_create_entry(
            title=f"Tariff Saver ({tariff_name})",
            data={
                "mode": MODE_PUBLIC,
                "tariff_name": tariff_name,
                "baseline_tariff_name": baseline_tariff_name,
                "publish_time": publish_time,
            },
        )

    async def async_step_oauth_start(self, user_input=None):
        """Start OAuth2 flow (will show Application Credentials if missing)."""
        return await super().async_step_user(user_input)

    async def async_oauth_create_entry(self, data: dict) -> config_entries.ConfigEntry:
        """Create the config entry after OAuth2 is complete."""
        return self.async_create_entry(
            title="Tariff Saver (myEKZ)",
            data={
                "mode": MODE_OAUTH,
                **data,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TariffSaverOptionsFlow(config_entry)


class TariffSaverOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Tariff Saver."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        return self.async_create_entry(title="", data={})
