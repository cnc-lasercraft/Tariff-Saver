"""Options flow for Tariff Saver."""
from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import (
    CONF_PUBLISH_TIME,
    DEFAULT_PUBLISH_TIME,
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_ENABLE_COST_TRACKING,
    DEFAULT_ENABLE_COST_TRACKING,
    CONF_GRADE_T1,
    CONF_GRADE_T2,
    CONF_GRADE_T3,
    CONF_GRADE_T4,
    DEFAULT_GRADE_T1,
    DEFAULT_GRADE_T2,
    DEFAULT_GRADE_T3,
    DEFAULT_GRADE_T4,
)


def _parse_hhmm(value: str) -> str:
    """Validate HH:MM."""
    try:
        hh, mm = value.strip().split(":")
        h = int(hh)
        m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except Exception as err:  # noqa: BLE001
        raise vol.Invalid("Time must be HH:MM") from err
    raise vol.Invalid("Time must be HH:MM")


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Tariff Saver.

    IMPORTANT:
    - HA calls TariffSaverOptionsFlowHandler(config_entry)
    - Do NOT assign self.config_entry (read-only property in your HA)
    - Store entry on our own attribute.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # Do not call super().__init__(config_entry) because in your HA it may take no args.
        # Do not set self.config_entry (read-only).
        self._ts_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate publish_time
            try:
                user_input[CONF_PUBLISH_TIME] = _parse_hhmm(user_input[CONF_PUBLISH_TIME])
            except vol.Invalid:
                errors[CONF_PUBLISH_TIME] = "invalid_time"

            # Validate threshold ordering
            try:
                t1 = float(user_input[CONF_GRADE_T1])
                t2 = float(user_input[CONF_GRADE_T2])
                t3 = float(user_input[CONF_GRADE_T3])
                t4 = float(user_input[CONF_GRADE_T4])
                if not (t1 <= t2 <= t3 <= t4):
                    errors[CONF_GRADE_T4] = "threshold_order"
            except Exception:  # noqa: BLE001
                errors[CONF_GRADE_T4] = "threshold_invalid"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        opt = dict(self._ts_entry.options)
        dat = dict(self._ts_entry.data)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PUBLISH_TIME,
                    default=opt.get(CONF_PUBLISH_TIME, dat.get(CONF_PUBLISH_TIME, DEFAULT_PUBLISH_TIME)),
                ): str,

                vol.Optional(
                    CONF_CONSUMPTION_ENERGY_ENTITY,
                    default=opt.get(CONF_CONSUMPTION_ENERGY_ENTITY, ""),
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),

                vol.Optional(
                    CONF_ENABLE_COST_TRACKING,
                    default=opt.get(CONF_ENABLE_COST_TRACKING, DEFAULT_ENABLE_COST_TRACKING),
                ): bool,

                vol.Required(CONF_GRADE_T1, default=opt.get(CONF_GRADE_T1, DEFAULT_GRADE_T1)): vol.Coerce(float),
                vol.Required(CONF_GRADE_T2, default=opt.get(CONF_GRADE_T2, DEFAULT_GRADE_T2)): vol.Coerce(float),
                vol.Required(CONF_GRADE_T3, default=opt.get(CONF_GRADE_T3, DEFAULT_GRADE_T3)): vol.Coerce(float),
                vol.Required(CONF_GRADE_T4, default=opt.get(CONF_GRADE_T4, DEFAULT_GRADE_T4)): vol.Coerce(float),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
