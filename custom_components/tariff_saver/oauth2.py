"""OAuth2 helpers for Tariff Saver."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow


async def async_get_config_entry_implementation(
    hass: HomeAssistant,
    config_entry,
) -> config_entry_oauth2_flow.OAuth2Implementation:
    """Return the OAuth2 implementation for this config entry."""
    return await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, config_entry
    )

