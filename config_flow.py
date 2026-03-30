"""Config flow for Elegant LED Controller integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_DEBOUNCE_ENABLED,
    CONF_EXTERNAL_CHANGE_DEBOUNCE,
    CONF_POLL_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_TIME_SYNC_ENABLED,
    CONF_TIME_SYNC_THRESHOLD,
    DEFAULT_DEBOUNCE_ENABLED,
    DEFAULT_EXTERNAL_CHANGE_DEBOUNCE,
    DEFAULT_HOST,
    DEFAULT_POLL_ENABLED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIME_SYNC_ENABLED,
    DEFAULT_TIME_SYNC_THRESHOLD,
    DOMAIN,
    EXTERNAL_CHANGE_DEBOUNCE_STEP,
    MAX_EXTERNAL_CHANGE_DEBOUNCE,
    MAX_POLL_INTERVAL,
    MAX_TIME_SYNC_THRESHOLD,
    MIN_EXTERNAL_CHANGE_DEBOUNCE,
    MIN_POLL_INTERVAL,
    MIN_TIME_SYNC_THRESHOLD,
)
from .coordinator import ElegantApiClient

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
    }
)


class ElegantConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Elegant LED Controller."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return ElegantOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            client = ElegantApiClient(host)

            try:
                await client.connect()
                settings = await client.get_user_settings()
            except (ConnectionError, TimeoutError, OSError) as err:
                _LOGGER.error("Failed to connect to %s: %s", host, err)
                errors["base"] = "cannot_connect"
            else:
                mac = settings.get("mac", "").replace(":", "").lower()
                sn = settings.get("sn", "")

                # Use MAC as unique ID
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

                title = f"Elegant {sn}" if sn else f"Elegant {host}"

                return self.async_create_entry(
                    title=title,
                    data={CONF_HOST: host},
                )
            finally:
                await client.disconnect()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class ElegantOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle options for Elegant LED Controller."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # Store enabled flags; if disabled, keep the numeric value but it
            # won't be used by the coordinator (coordinator checks *_enabled).
            return self.async_create_entry(title="", data=user_input)

        # Current values (with backwards compat for old "0 = disabled" format)
        cur_ts_enabled = self.options.get(
            CONF_TIME_SYNC_ENABLED, DEFAULT_TIME_SYNC_ENABLED
        )
        cur_ts_value = self.options.get(
            CONF_TIME_SYNC_THRESHOLD, DEFAULT_TIME_SYNC_THRESHOLD
        )
        # Backwards compat: old config stored 0 as "disabled"
        if isinstance(cur_ts_value, (int, float)) and cur_ts_value == 0:
            cur_ts_enabled = False
            cur_ts_value = DEFAULT_TIME_SYNC_THRESHOLD

        cur_poll_enabled = self.options.get(
            CONF_POLL_ENABLED, DEFAULT_POLL_ENABLED
        )
        cur_poll_value = self.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        if isinstance(cur_poll_value, (int, float)) and cur_poll_value == 0:
            cur_poll_enabled = False
            cur_poll_value = DEFAULT_POLL_INTERVAL

        cur_deb_enabled = self.options.get(
            CONF_DEBOUNCE_ENABLED, DEFAULT_DEBOUNCE_ENABLED
        )
        cur_deb_value = self.options.get(
            CONF_EXTERNAL_CHANGE_DEBOUNCE, DEFAULT_EXTERNAL_CHANGE_DEBOUNCE
        )
        if isinstance(cur_deb_value, (int, float)) and cur_deb_value == 0:
            cur_deb_enabled = False
            cur_deb_value = DEFAULT_EXTERNAL_CHANGE_DEBOUNCE

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TIME_SYNC_ENABLED,
                        default=cur_ts_enabled,
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_TIME_SYNC_THRESHOLD,
                        default=cur_ts_value,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_TIME_SYNC_THRESHOLD,
                            max=MAX_TIME_SYNC_THRESHOLD,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_POLL_ENABLED,
                        default=cur_poll_enabled,
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=cur_poll_value,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL,
                            max=MAX_POLL_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_DEBOUNCE_ENABLED,
                        default=cur_deb_enabled,
                    ): BooleanSelector(),
                    vol.Required(
                        CONF_EXTERNAL_CHANGE_DEBOUNCE,
                        default=cur_deb_value,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_EXTERNAL_CHANGE_DEBOUNCE,
                            max=MAX_EXTERNAL_CHANGE_DEBOUNCE,
                            step=EXTERNAL_CHANGE_DEBOUNCE_STEP,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
