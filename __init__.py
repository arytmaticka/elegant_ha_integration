"""The Elegant LED Controller integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    ATTR_ZONE_INDEX,
    CONF_DEBOUNCE_ENABLED,
    CONF_EXTERNAL_CHANGE_DEBOUNCE,
    CONF_POLL_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_TIME_SYNC_ENABLED,
    CONF_TIME_SYNC_THRESHOLD,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR,
    DEFAULT_COLOR_HUE,
    DEFAULT_COLOR_MODE,
    DEFAULT_COLOR_SATURATION,
    DEFAULT_DEBOUNCE_ENABLED,
    DEFAULT_EXTERNAL_CHANGE_DEBOUNCE,
    DEFAULT_POLL_ENABLED,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIME_SYNC_ENABLED,
    DEFAULT_TIME_SYNC_THRESHOLD,
    DEFAULT_WHITE_TEMPERATURE,
    DOMAIN,
    MAX_ZONES,
    SERVICE_RESET_ZONE_DEFAULTS,
)
from .coordinator import ElegantCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SENSOR]

RESET_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ZONE_INDEX): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_ZONES - 1)
        ),
    }
)


def _effective_options(options: dict) -> tuple[int, int, float]:
    """Extract effective numeric values from options, respecting enabled flags.

    Returns (time_sync_threshold, poll_interval, external_change_debounce).
    A value of 0 means the feature is disabled.
    """
    ts_enabled = options.get(CONF_TIME_SYNC_ENABLED, DEFAULT_TIME_SYNC_ENABLED)
    ts_value = int(options.get(CONF_TIME_SYNC_THRESHOLD, DEFAULT_TIME_SYNC_THRESHOLD))
    time_sync = ts_value if ts_enabled else 0

    poll_enabled = options.get(CONF_POLL_ENABLED, DEFAULT_POLL_ENABLED)
    poll_value = int(options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    poll = poll_value if poll_enabled else 0

    deb_enabled = options.get(CONF_DEBOUNCE_ENABLED, DEFAULT_DEBOUNCE_ENABLED)
    deb_value = float(options.get(CONF_EXTERNAL_CHANGE_DEBOUNCE, DEFAULT_EXTERNAL_CHANGE_DEBOUNCE))
    debounce = deb_value if deb_enabled else 0.0

    return time_sync, poll, debounce


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Elegant LED Controller from a config entry."""
    host = entry.data[CONF_HOST]
    time_sync, poll, debounce = _effective_options(entry.options)

    coordinator = ElegantCoordinator(
        hass, host, time_sync, poll, debounce
    )

    try:
        await coordinator.async_setup()
    except (ConnectionError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            f"Failed to connect to Elegant controller at {host}"
        ) from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # Register services
    _register_services(hass)

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: ElegantCoordinator = hass.data[DOMAIN][entry.entry_id]
    time_sync, poll, debounce = _effective_options(entry.options)

    coordinator.time_sync_threshold = time_sync

    poll_changed = poll != coordinator.poll_interval
    coordinator.poll_interval = poll

    coordinator.external_change_debounce = debounce

    # Restart poll loop if interval changed
    if poll_changed:
        coordinator._start_poll_loop()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: ElegantCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    # Unregister services if no more entries
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RESET_ZONE_DEFAULTS)

    return unload_ok


def _register_services(hass: HomeAssistant) -> None:
    """Register custom services."""

    if hass.services.has_service(DOMAIN, SERVICE_RESET_ZONE_DEFAULTS):
        return

    async def handle_reset_zone_defaults(call: ServiceCall) -> None:
        """Handle reset_zone_defaults service call."""
        zone_index = call.data[ATTR_ZONE_INDEX]
        # Find the first available coordinator
        for coordinator in hass.data.get(DOMAIN, {}).values():
            if isinstance(coordinator, ElegantCoordinator):
                await coordinator.async_set_zone(
                    zone_index,
                    color_mode=DEFAULT_COLOR_MODE,
                    color_1=DEFAULT_COLOR,
                    color_1_hue=DEFAULT_COLOR_HUE,
                    color_2=DEFAULT_COLOR,
                    color_2_hue=DEFAULT_COLOR_HUE,
                    color_3=DEFAULT_COLOR,
                    color_3_hue=DEFAULT_COLOR_HUE,
                    white_temperature=DEFAULT_WHITE_TEMPERATURE,
                    color_saturation=DEFAULT_COLOR_SATURATION,
                    bright=DEFAULT_BRIGHTNESS,
                )
                return

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_ZONE_DEFAULTS,
        handle_reset_zone_defaults,
        schema=RESET_ZONE_SCHEMA,
    )



