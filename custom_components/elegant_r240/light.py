"""Light platform for Elegant LED Controller integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)

# Light attribute keys — defined as strings for compatibility
# with newer HA versions that removed ATTR_* constants from light module
ATTR_BRIGHTNESS = "brightness"
ATTR_HS_COLOR = "hs_color"
ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ZONE_INDEX,
    ATTR_ZONE_TYPE,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR,
    DEFAULT_COLOR_HUE,
    DEFAULT_COLOR_MODE,
    DEFAULT_COLOR_SATURATION,
    DEFAULT_WHITE_TEMPERATURE,
    DOMAIN,
    ELEGANT_HUE_MAX,
    MAX_COLOR_TEMP_KELVIN,
    MAX_ZONES,
    MIN_COLOR_TEMP_KELVIN,
)
from .coordinator import ElegantCoordinator

_LOGGER = logging.getLogger(__name__)


def _controller_name(coordinator: ElegantCoordinator) -> str:
    """Return a stable display name for the controller device."""
    serial = coordinator.user_settings.get("sn")
    serial_text = "" if serial is None else str(serial)
    return f"Elegant-{serial_text[-4:]}" if serial_text else "Elegant"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Elegant lights from a config entry."""
    coordinator: ElegantCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for idx in range(min(len(coordinator.zones), MAX_ZONES)):
        zone = coordinator.zones[idx]
        entities.append(ElegantLight(coordinator, entry, idx, zone))

    async_add_entities(entities)


def _elegant_temp_to_kelvin(elegant_temp: int) -> int:
    """Convert Elegant white_temperature (0-100) to Kelvin.

    0 = warm white (2700K)
    100 = cool white (6500K)
    """
    return int(MIN_COLOR_TEMP_KELVIN + (elegant_temp / 100) * (MAX_COLOR_TEMP_KELVIN - MIN_COLOR_TEMP_KELVIN))


def _kelvin_to_elegant_temp(kelvin: int) -> int:
    """Convert Kelvin to Elegant white_temperature (0-100)."""
    value = int(((kelvin - MIN_COLOR_TEMP_KELVIN) / (MAX_COLOR_TEMP_KELVIN - MIN_COLOR_TEMP_KELVIN)) * 100)
    return max(0, min(100, value))


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color string (#RRGGBB or 0xRRGGBB) to RGB tuple."""
    if hex_color.startswith("#"):
        hex_color = hex_color[1:]
    elif hex_color.startswith("0x") or hex_color.startswith("0X"):
        hex_color = hex_color[2:]
    hex_color = hex_color.zfill(6)
    return (
        int(hex_color[0:2], 16),
        int(hex_color[2:4], 16),
        int(hex_color[4:6], 16),
    )


def _rgb_to_hex_0x(r: int, g: int, b: int) -> str:
    """Convert RGB tuple to 0xRRGGBB hex string (Elegant format for sending)."""
    return f"0x{r:02X}{g:02X}{b:02X}"


def _rgb_to_hs(r: int, g: int, b: int) -> tuple[float, float]:
    """Convert RGB to HS (hue 0-360, saturation 0-100)."""
    r_f, g_f, b_f = r / 255.0, g / 255.0, b / 255.0
    max_c = max(r_f, g_f, b_f)
    min_c = min(r_f, g_f, b_f)
    diff = max_c - min_c

    if diff == 0:
        hue = 0.0
    elif max_c == r_f:
        hue = (60 * ((g_f - b_f) / diff) + 360) % 360
    elif max_c == g_f:
        hue = (60 * ((b_f - r_f) / diff) + 120) % 360
    else:
        hue = (60 * ((r_f - g_f) / diff) + 240) % 360

    saturation = 0.0 if max_c == 0 else (diff / max_c) * 100

    return (hue, saturation)


def _hs_to_rgb(hue: float, saturation: float) -> tuple[int, int, int]:
    """Convert HS (hue 0-360, saturation 0-100) to RGB."""
    hue = hue % 360  # Normalize: 360° == 0° (red)
    s = saturation / 100.0
    v = 1.0  # Full value/brightness — brightness is handled separately
    c = v * s
    x = c * (1 - abs((hue / 60) % 2 - 1))
    m = v - c

    if hue < 60:
        r_f, g_f, b_f = c, x, 0
    elif hue < 120:
        r_f, g_f, b_f = x, c, 0
    elif hue < 180:
        r_f, g_f, b_f = 0, c, x
    elif hue < 240:
        r_f, g_f, b_f = 0, x, c
    elif hue < 300:
        r_f, g_f, b_f = x, 0, c
    else:
        r_f, g_f, b_f = c, 0, x

    return (
        int((r_f + m) * 255),
        int((g_f + m) * 255),
        int((b_f + m) * 255),
    )


def _decode_scenes_to_effect_ids(scenes: list[int] | tuple[int, ...]) -> list[int]:
    """Decode 4x uint32 bitfield into sorted list of enabled effect IDs (0..127)."""
    if not isinstance(scenes, (list, tuple)):
        return []

    effect_ids: list[int] = []
    for word_index, raw_word in enumerate(scenes[:4]):
        try:
            word = int(raw_word) & 0xFFFFFFFF
        except (TypeError, ValueError):
            continue

        for bit_index in range(32):
            if word & (1 << bit_index):
                effect_ids.append(word_index * 32 + bit_index)

    return effect_ids


def _encode_effect_id_to_scenes(effect_id: int) -> list[int]:
    """Encode single effect_id (0..127) into 4x uint32 bitfield."""
    if effect_id < 0 or effect_id > 127:
        raise ValueError(f"effect_id out of range: {effect_id}")

    scenes = [0, 0, 0, 0]
    word_idx = effect_id // 32
    bit_idx = effect_id % 32
    scenes[word_idx] = 1 << bit_idx
    return scenes


class ElegantLight(CoordinatorEntity, LightEntity):
    """Representation of an Elegant LED zone as a light entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ElegantCoordinator,
        entry: ConfigEntry,
        idx: int,
        zone_data: dict,
    ) -> None:
        """Initialize the light entity."""
        super().__init__(coordinator)
        self._idx = idx
        self._zone_type = zone_data.get("type", 0)
        self._attr_unique_id = f"{coordinator.mac}_{idx}"
        self._attr_name = zone_data.get("name", f"Elegant Room {idx + 1}")

        # Device info — all zones belong to one controller device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.mac)},
            name=_controller_name(coordinator),
            manufacturer="Elegant",
            model="LED Controller",
            configuration_url=f"http://{coordinator.host}",
        )

        # Set supported color modes based on zone type
        if self._zone_type > 0:
            # Physical zone: supports HS color and color temp
            self._attr_supported_color_modes = {
                ColorMode.HS,
                ColorMode.COLOR_TEMP,
            }
            self._attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
            self._attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
            self._attr_entity_registry_enabled_default = True
        else:
            # Virtual zone (type 0): on/off only — serves as trigger for automations
            # Disabled by default, user can enable manually in HA UI
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_entity_registry_enabled_default = False

    @property
    def _zone(self) -> dict:
        """Return current zone data from coordinator."""
        if self.coordinator.zones and self._idx < len(self.coordinator.zones):
            return self.coordinator.zones[self._idx]
        return {}

    @property
    def is_on(self) -> bool:
        """Return true if the light is on."""
        return self._zone.get("is_on", False)

    @property
    def brightness(self) -> int | None:
        """Return the brightness (0-255)."""
        if self._zone_type == 0:
            return None
        bright = self._zone.get("bright", 100)
        # Elegant uses 0-100, HA uses 0-255
        ha_bright = int(bright * 255 / 100)
        # HA expects 1-255 for an "on" light; 0 means off
        return max(1, ha_bright) if bright > 0 else 0

    @property
    def color_mode(self) -> ColorMode:
        """Return the current color mode."""
        if self._zone_type == 0:
            return ColorMode.ONOFF

        elegant_mode = self._zone.get("color_mode", 0)
        saturation = self._zone.get("color_saturation", 0)

        # If saturation is 0, it's white/color temp mode
        if saturation == 0 and elegant_mode == 0:
            return ColorMode.COLOR_TEMP
        return ColorMode.HS

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return the HS color value."""
        if self._zone_type == 0:
            return None
        if self.color_mode != ColorMode.HS:
            return None
        # The controller returns color_1_hue in degrees (0-360) in push responses
        hue = self._zone.get("color_1_hue", 0)
        # If hue is 0, try to derive from hex color
        if hue == 0:
            color_hex = self._zone.get("color_1", "#FFFFFF")
            r, g, b = _hex_to_rgb(color_hex)
            hue, _ = _rgb_to_hs(r, g, b)
        elegant_sat = self._zone.get("color_saturation", 100)
        return (float(hue), float(elegant_sat))

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin."""
        if self._zone_type == 0:
            return None
        if self.color_mode != ColorMode.COLOR_TEMP:
            return None
        elegant_temp = self._zone.get("white_temperature", 50)
        return _elegant_temp_to_kelvin(elegant_temp)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {
            ATTR_ZONE_INDEX: self._idx,
            ATTR_ZONE_TYPE: self._zone_type,
        }
        # Include raw elegant color_mode for debugging/automations
        attrs["elegant_color_mode"] = self._zone.get("color_mode", 0)
        return attrs

    @property
    def supported_features(self) -> LightEntityFeature:
        features = LightEntityFeature(0)

        if self._zone_type != 0:
            features |= LightEntityFeature.TRANSITION

            effects = self._zone.get("available_effects", {})
            if effects:
                features |= LightEntityFeature.EFFECT

        _LOGGER.debug("Supported features for zone_id=%d: %s", self._idx, features)

        return features

    @property
    def effect_list(self) -> list[str]:
        effects: dict[int, str] = self._zone.get("available_effects", {})
        _LOGGER.debug("Effect list for zone_id=%d: %s", self._idx, effects)
        
        return list(effects.values())

    @property
    def effect(self) -> str | None:
        effects: dict[int, str] = self._zone.get("available_effects", {})
        scenes = self._zone.get("scenes")
        if not scenes:
            return None

        enabled_effect_ids = _decode_scenes_to_effect_ids(scenes)
        if not enabled_effect_ids:
            return None

        effect_id = min(enabled_effect_ids)

        return effects.get(effect_id) or effects.get(str(effect_id))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on with optional parameters."""
        params: dict[str, Any] = {}
        
        if ATTR_EFFECT in kwargs:
            selected_name = kwargs[ATTR_EFFECT]

            effects_map = self._zone.get("available_effects", {})
            effect_id = next(
                (int(eid) for eid, name in effects_map.items() if name == selected_name),
                None,
            )

            if effect_id is None:
                _LOGGER.warning("Effect not found: %s", selected_name)
            else:
                scenes = _encode_effect_id_to_scenes(effect_id)
                #_LOGGER.debug("Setting effect '%s' (id %d) for zone %d: scenes=%s", selected_name, effect_id, self._idx, scenes)
                params["scenes"] = scenes  


        if ATTR_BRIGHTNESS in kwargs:
            # HA 0-255 -> Elegant 0-100
            ha_bright = kwargs[ATTR_BRIGHTNESS]
            params["bright"] = max(1, int(ha_bright * 100 / 255))

        if ATTR_HS_COLOR in kwargs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            # R240 expects color_1 on the color wheel edge; saturation is separate.
            r, g, b = _hs_to_rgb(hue, 100)
            # Elegant SEND uses hue in 0-255 scale: elegant_hue = ha_hue * 256 / 360
            elegant_hue = int(hue * ELEGANT_HUE_MAX / 360)
            params["color_1"] = _rgb_to_hex_0x(r, g, b)
            params["color_1_hue"] = elegant_hue
            params["color_saturation"] = int(saturation)
            params["color_mode"] = 0  # Static color mode

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            elegant_temp = _kelvin_to_elegant_temp(kelvin)
            params["white_temperature"] = elegant_temp
            # Switch to white mode: color_mode 0 with saturation 0
            params["color_saturation"] = 0
            params["color_mode"] = 0
            params["color_1"] = DEFAULT_COLOR
            params["color_1_hue"] = 0

        # Always ensure the light is turned on
        params["is_on"] = True

        await self.coordinator.async_set_zone(self._idx, **params)
        # await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.async_set_zone(self._idx, is_on=False)

    async def async_reset_to_defaults(self) -> None:
        """Reset zone to defaults: white neutral, no effects."""
        await self.coordinator.async_set_zone(
            self._idx,
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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()






