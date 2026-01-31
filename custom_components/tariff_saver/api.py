"""API client for EKZ tariffs."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class EkzTariffApi:
    """Simple client for the EKZ tariff API."""

    BASE_URL = "https://api.tariffs.ekz.ch/v1"

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def fetch_tariffs(
        self,
        tariff_name: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch tariff prices from EKZ API."""
        params = {
            "tariff_name": tariff_name,
            "start_timestamp": start.isoformat(),
            "end_timestamp": end.isoformat(),
        }

        url = f"{self.BASE_URL}/tariffs"

        _LOGGER.debug("Fetching EKZ tariffs: %s", params)

        async with self._session.get(url, params=params, timeout=30) as response:
            response.raise_for_status()
            data = await response.json()

        return data
