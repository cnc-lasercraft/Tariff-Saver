"""Application credentials support for Tariff Saver (myEKZ OAuth2).

Stores client_id/client_secret in Home Assistant's Application Credentials UI.
"""
from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer, ClientCredential
from homeassistant.core import HomeAssistant

from .const import DOMAIN

AUTH_BASE = "https://login.ekz.ch/auth"
REALM = "myEKZ"

AUTH_URL = f"{AUTH_BASE}/realms/{REALM}/protocol/openid-connect/auth"
TOKEN_URL = f"{AUTH_BASE}/realms/{REALM}/protocol/openid-connect/token"


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the authorization server for myEKZ (Keycloak)."""
    return AuthorizationServer(
        authorize_url=AUTH_URL,
        token_url=TOKEN_URL,
    )


async def async_get_client_credential(hass: HomeAssistant) -> ClientCredential:
    """Return the client credential name shown in HA UI."""
    return ClientCredential(
        name="myEKZ (Tariff Saver)",
        domain=DOMAIN,
    )
