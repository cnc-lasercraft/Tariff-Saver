"""API client for EKZ tariffs (Public + myEKZ OAuth2).

Public:
- GET /v1/tariffs  (returns {prices:[...]})

Protected (OAuth2 Bearer):
- GET /v1/emsLinkStatus
- GET /v1/customerTariffs

IMPORTANT:
- No entities are defined here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Final

import aiohttp
from aiohttp import ClientError

from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

_LOGGER = logging.getLogger(__name__)


class EkzTariffApiError(RuntimeError):
    """Raised when EKZ API calls fail."""


class EkzTariffAuthError(EkzTariffApiError):
    """Raised for OAuth/token problems."""


class EkzTariffApi:
    """Client for the EKZ tariff API."""

    BASE_URL: Final[str] = "https://api.tariffs.ekz.ch/v1"

    # Components we know from Swagger (some may be missing per tariff)
    COMPONENT_KEYS: Final[list[str]] = [
        "electricity",
        "grid",
        "integrated",
        "regional_fees",
        "metering",
        "refund_storage",
        "feed_in",
    ]

    # Accept common unit spellings (Swagger shows CHF_kWh, some APIs use CHF/kWh)
    _CHF_PER_KWH_UNITS: Final[set[str]] = {"CHF_kWh", "CHF/kWh", "CHF_PER_KWH", "CHF_PER_KW_H", "CHF_KWH"}

    def __init__(
        self,
        session: aiohttp.ClientSession,
        oauth_session: OAuth2Session | None = None,
    ) -> None:
        self._session = session
        self._oauth_session = oauth_session

    # ---------------------------------------------------------------------
    # Public
    # ---------------------------------------------------------------------
    async def fetch_prices(
        self,
        tariff_name: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch raw price items (entries from `prices`)."""
        params: dict[str, Any] = {"tariff_name": tariff_name}

        if start is not None and end is not None:
            params["start_timestamp"] = start.isoformat()
            params["end_timestamp"] = end.isoformat()

        url = f"{self.BASE_URL}/tariffs"
        _LOGGER.debug("Fetching EKZ tariffs: %s", params)

        async with self._session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            resp.raise_for_status()
            payload: dict[str, Any] = await resp.json()

        prices = payload.get("prices")
        if not isinstance(prices, list):
            raise ValueError(f"Unexpected EKZ payload shape, missing 'prices': {payload!r}")

        return prices

    # ---------------------------------------------------------------------
    # Helpers: parse component prices robustly
    # ---------------------------------------------------------------------
    @classmethod
    def _extract_value_chf_per_kwh(cls, val: Any) -> float | None:
        """Return CHF/kWh float if present, otherwise None.

        Handles common shapes:
        - float/int (assumed CHF/kWh)
        - {"unit":"CHF_kWh","value":0.123}
        - {"value":0.123,"unit":"CHF/kWh"}
        - [{"unit":"CHF_kWh","value":0.1}, ...]  (legacy list form)
        """
        if isinstance(val, (int, float)):
            return float(val)

        if isinstance(val, dict):
            unit = val.get("unit")
            # If unit provided and not CHF/kWh -> reject
            if isinstance(unit, str) and unit and unit not in cls._CHF_PER_KWH_UNITS:
                return None

            for k in ("value", "amount", "price", "chf_per_kwh"):
                v = val.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
            return None

        if isinstance(val, list):
            total = 0.0
            found = False
            for entry in val:
                if not isinstance(entry, dict):
                    continue
                unit = entry.get("unit")
                if isinstance(unit, str) and unit not in cls._CHF_PER_KWH_UNITS:
                    continue
                v = entry.get("value")
                if isinstance(v, (int, float)):
                    total += float(v)
                    found = True
            return total if found else None

        return None

    @classmethod
    def parse_components_chf_per_kwh(cls, price_item: dict[str, Any]) -> dict[str, float]:
        """Extract known component prices (CHF/kWh) from a price item."""
        out: dict[str, float] = {}
        for key in cls.COMPONENT_KEYS:
            if key not in price_item:
                continue
            v = cls._extract_value_chf_per_kwh(price_item.get(key))
            if isinstance(v, (int, float)) and v != 0.0:
                out[key] = float(v)
        return out

    # ---------------------------------------------------------------------
    # Price helper (USED BY COORDINATOR)
    # ---------------------------------------------------------------------
    @classmethod
    def sum_chf_per_kwh(cls, price_item: dict[str, Any]) -> float:
        """Return electricity-only CHF/kWh if available, else sum of all components."""
        comps = cls.parse_components_chf_per_kwh(price_item)

        # keep legacy behavior: "Price now" is electricity-only
        if "electricity" in comps:
            return float(comps["electricity"])

        # fallback: sum everything we have (still better than 0)
        return float(sum(comps.values())) if comps else 0.0

    # ---------------------------------------------------------------------
    # Protected (myEKZ)
    # ---------------------------------------------------------------------
    async def _async_get_access_token(self) -> str:
        if not self._oauth_session:
            raise EkzTariffAuthError("No OAuth session available (myEKZ not configured)")

        try:
            await self._oauth_session.async_ensure_token_valid()
        except Exception as err:
            raise EkzTariffAuthError(f"OAuth token invalid/refresh failed: {err}") from err

        token = self._oauth_session.token or {}
        access_token = token.get("access_token")
        if not access_token:
            raise EkzTariffAuthError("OAuth token missing access_token")
        return access_token

    async def fetch_ems_link_status(self, *, ems_instance_id: str, redirect_uri: str) -> dict[str, Any]:
        access_token = await self._async_get_access_token()

        url = f"{self.BASE_URL}/emsLinkStatus"
        params = {"ems_instance_id": ems_instance_id, "redirect_uri": redirect_uri}
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise EkzTariffAuthError(f"401 Unauthorized: {text}")
                if resp.status >= 400:
                    raise EkzTariffApiError(f"EKZ API error {resp.status}: {text}")
                data: Any = await resp.json()
        except ClientError as err:
            raise EkzTariffApiError(f"HTTP error calling emsLinkStatus: {err}") from err

        if not isinstance(data, dict):
            raise EkzTariffApiError(f"Unexpected emsLinkStatus payload: {data!r}")
        return data

    async def fetch_customer_tariffs(
        self,
        *,
        ems_instance_id: str,
        tariff_type: str | None = None,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> list[dict[str, Any]]:
        access_token = await self._async_get_access_token()

        url = f"{self.BASE_URL}/customerTariffs"
        params: dict[str, str] = {"ems_instance_id": ems_instance_id}
        if tariff_type:
            params["tariff_type"] = tariff_type
        if start_timestamp:
            params["start_timestamp"] = start_timestamp
        if end_timestamp:
            params["end_timestamp"] = end_timestamp

        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise EkzTariffAuthError(f"401 Unauthorized: {text}")
                if resp.status >= 400:
                    raise EkzTariffApiError(f"EKZ API error {resp.status}: {text}")
                data: Any = await resp.json()
        except ClientError as err:
            raise EkzTariffApiError(f"HTTP error calling customerTariffs: {err}") from err

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            tariffs = data.get("tariffs")
            if isinstance(tariffs, list):
                return tariffs
        raise EkzTariffApiError(f"Unexpected customerTariffs payload: {data!r}")
