"""The Elegant LED Controller integration."""

from __future__ import annotations

import logging
import os
import shutil

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    ATTR_EFFECTS,
    ATTR_EFFECT_IDS,
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
    SERVICE_SET_ZONE_EFFECTS,
)
from .coordinator import ElegantCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SENSOR]

CARD_JS_FILENAME = "elegant-effects-card.js"
CARD_JS_URL = f"/local/{CARD_JS_FILENAME}"

RESET_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ZONE_INDEX): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_ZONES - 1)
        ),
    }
)

SET_ZONE_EFFECTS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ZONE_INDEX): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_ZONES - 1)
        ),
        # Either `effects` (list of names) OR `effect_ids` (list of ints).
        # `effect_ids` takes precedence when both are provided.
        vol.Optional(ATTR_EFFECTS): vol.All(vol.Coerce(list), [str]),
        vol.Optional(ATTR_EFFECT_IDS): vol.All(
            vol.Coerce(list), [vol.All(vol.Coerce(int), vol.Range(min=0, max=255))]
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

    # Register Lovelace card frontend
    await _async_register_frontend(hass)

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
        hass.services.async_remove(DOMAIN, SERVICE_SET_ZONE_EFFECTS)

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

    # --- set_zone_effects service ---

    if hass.services.has_service(DOMAIN, SERVICE_SET_ZONE_EFFECTS):
        return

    async def handle_set_zone_effects(call: ServiceCall) -> None:
        """Handle set_zone_effects service call — set multiple effects on a zone.

        Accepts either:
          - effect_ids: list[int]  — real effect IDs (== bit positions, 0–127).
            Preferred: bypasses name lookup, works across controller types.
          - effects:    list[str]  — effect names, resolved via zone's
            available_effects dictionary.
        """
        zone_index = call.data[ATTR_ZONE_INDEX]
        effect_ids_raw = call.data.get(ATTR_EFFECT_IDS)
        effect_names = call.data.get(ATTR_EFFECTS)

        if effect_ids_raw is None and effect_names is None:
            _LOGGER.warning(
                "set_zone_effects: neither 'effect_ids' nor 'effects' provided"
            )
            return

        for coordinator in hass.data.get(DOMAIN, {}).values():
            if not isinstance(coordinator, ElegantCoordinator):
                continue

            from .light import _encode_effect_ids_to_scenes

            zones = coordinator.zones
            if zone_index >= len(zones):
                _LOGGER.warning("Zone index %d out of range", zone_index)
                return

            # Path 1 — explicit IDs (preferred)
            if effect_ids_raw is not None:
                ids = list(effect_ids_raw)
                if not ids:
                    await coordinator.async_set_zone(
                        zone_index, scenes=[0, 0, 0, 0]
                    )
                    return
                scenes = _encode_effect_ids_to_scenes(ids)
                await coordinator.async_set_zone(zone_index, scenes=scenes)
                return

            # Path 2 — resolve names via THIS zone's dictionary only
            # (names are NOT shared across controller types)
            if not effect_names:
                await coordinator.async_set_zone(
                    zone_index, scenes=[0, 0, 0, 0]
                )
                return

            zone = zones[zone_index]
            effects_map = zone.get("available_effects", {})

            resolved_ids: list[int] = []
            for name in effect_names:
                eid = next(
                    (int(k) for k, v in effects_map.items() if v == name),
                    None,
                )
                if eid is not None:
                    resolved_ids.append(eid)
                else:
                    _LOGGER.warning(
                        "Effect '%s' not found for zone %d", name, zone_index
                    )

            if not resolved_ids:
                _LOGGER.warning(
                    "No valid effects resolved for zone %d", zone_index
                )
                return

            scenes = _encode_effect_ids_to_scenes(resolved_ids)
            await coordinator.async_set_zone(zone_index, scenes=scenes)
            return

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ZONE_EFFECTS,
        handle_set_zone_effects,
        schema=SET_ZONE_EFFECTS_SCHEMA,
    )


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the Elegant Effects Card by copying JS to config/www/.

    The config/www/ directory is always served by HA under /local/.
    This is the most reliable method — no register_static_path needed.
    """
    if hass.data.get(f"{DOMAIN}_frontend"):
        return
    hass.data[f"{DOMAIN}_frontend"] = True

    src = os.path.join(os.path.dirname(__file__), CARD_JS_FILENAME)
    www_dir = hass.config.path("www")
    dst = os.path.join(www_dir, CARD_JS_FILENAME)

    if not os.path.isfile(src):
        _LOGGER.warning(
            "Elegant Effects Card: source JS not found at %s", src
        )
        return

    # Copy file in executor to avoid blocking the event loop
    def _copy_card():
        os.makedirs(www_dir, exist_ok=True)
        shutil.copy2(src, dst)

    try:
        await hass.async_add_executor_job(_copy_card)
        _LOGGER.warning("Elegant Effects Card: JS copied to %s", dst)
    except OSError as err:
        _LOGGER.warning(
            "Elegant Effects Card: failed to copy JS to %s: %s", dst, err
        )
        return

    # --- Register as Lovelace resource programmatically ---
    try:
        await _async_register_lovelace_resource(hass, CARD_JS_URL)
    except Exception as err:
        _LOGGER.warning(
            "Elegant Effects Card: Lovelace resource registration failed: %s. "
            "Add %s as a Lovelace resource (type: module) manually in "
            "Settings › Dashboards › Resources.",
            err,
            CARD_JS_URL,
        )


async def _async_register_lovelace_resource(
    hass: HomeAssistant, url: str
) -> None:
    """Register a JS module as a Lovelace dashboard resource."""
    # Try the frontend extra_js approach first
    try:
        add_extra_js_url(hass, url)
        _LOGGER.warning(
            "Elegant Effects Card: registered via add_extra_js_url at %s. "
            "Hard-refresh browser (Ctrl+Shift+R).",
            url,
        )
        return
    except Exception:
        _LOGGER.debug("add_extra_js_url failed, trying lovelace resources API")

    # Fallback: use Lovelace resource storage (like HACS does)
    try:
        from homeassistant.components.lovelace import ResourceStorageCollection

        resources: ResourceStorageCollection | None = hass.data.get(
            "lovelace_resources"
        )
        if resources is None:
            _LOGGER.warning(
                "Elegant Effects Card: lovelace_resources not available "
                "(dashboard may be in YAML mode). "
                "Add %s as a resource manually.",
                url,
            )
            return

        # Check if already registered
        for item in resources.async_items():
            if item.get("url") == url:
                _LOGGER.warning(
                    "Elegant Effects Card: already registered as Lovelace resource at %s",
                    url,
                )
                return

        # Register new resource
        await resources.async_create_item({"res_type": "module", "url": url})
        _LOGGER.warning(
            "Elegant Effects Card: registered as Lovelace resource at %s. "
            "Hard-refresh browser (Ctrl+Shift+R).",
            url,
        )
    except ImportError:
        _LOGGER.warning(
            "Elegant Effects Card: ResourceStorageCollection not available. "
            "Add %s as a resource (type: module) manually.",
            url,
        )
    except Exception as err:
        raise RuntimeError(f"Lovelace resource API failed: {err}") from err



