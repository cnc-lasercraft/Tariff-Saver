"""Options flow for Tariff Saver."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CONSUMPTION_ENERGY_ENTITY,
    CONF_GRADE_THRESHOLDS,
    DEFAULT_GRADE_THRESHOLDS,
)


def _validate_thresholds(t1: float, t2: float, t3: float, t4: float) -> None:
    # Must be strictly increasing to avoid overlaps/holes
    if not (t1 < t2 < t3 < t4):
        raise vol.Invalid("Thresholds must be strictly increasing (t1 < t2 < t3 < t4).")


class TariffSaverOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for Tariff Saver."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        opts = dict(self.config_entry.options)

        current_energy = opts.get(CONF_CONSUMPTION_ENERGY_ENTITY, "")
        thresholds = opts.get(CONF_GRADE_THRESHOLDS, DEFAULT_GRADE_THRESHOLDS)
        # Defensive: if stored wrong type
        if not isinstance(thresholds, list) or len(thresholds) != 4:
            thresholds = DEFAULT_GRADE_THRESHOLDS

        t1, t2, t3, t4 = [float(x) for x in thresholds]

        if user_input is not None:
            # Read + validate
            energy_entity = user_input.get(CONF_CONSUMPTION_ENERGY_ENTITY) or ""
            nt1 = float(user_input["grade_t1"])
            nt2 = float(user_input["grade_t2"])
            nt3 = float(user_input["grade_t3"])
            nt4 = float(user_input["grade_t4"])

            try:
                _validate_thresholds(nt1, nt2, nt3, nt4)
            except vol.Invalid:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self._schema(current_energy, nt1, nt2, nt3, nt4),
                    errors={"base": "invalid_thresholds"},
                )

            return self.async_create_entry(
                title="",
                data={
                    CONF_CONSUMPTION_ENERGY_ENTITY: energy_entity,
                    CONF_GRADE_THRESHOLDS: [nt1, nt2, nt3, nt4],
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(current_energy, t1, t2, t3, t4),
        )

    @staticmethod
    def _schema(energy_entity: str, t1: float, t2: float, t3: float, t4: float) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(CONF_CONSUMPTION_ENERGY_ENTITY, default=energy_entity): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                    )
                ),

                # Thresholds in percent vs daily average
                vol.Required("grade_t1", default=t1): vol.Coerce(float),
                vol.Required("grade_t2", default=t2): vol.Coerce(float),
                vol.Required("grade_t3", default=t3): vol.Coerce(float),
                vol.Required("grade_t4", default=t4): vol.Coerce(float),
            }
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TariffSaverOptionsFlowHandler(config_entry)
