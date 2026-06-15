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
    ATTR_ACTIVE_MODE,
    ATTR_ACTIVE_MODE_ID,
    ATTR_AVAILABLE_MODES,
    ATTR_EFFECT_FLAGS,
    ATTR_ZONE_INDEX,
    ATTR_ZONE_TYPE,
    COLOR_MODE_AUTO_CHANGE,
    COLOR_MODE_DOUBLE,
    COLOR_MODE_RAINBOW,
    COLOR_MODE_SINGLE,
    COLOR_MODE_TRIPLE,
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
    MODE_KEY_AUTO_CHANGE,
    MODE_KEY_COLOR,
    MODE_KEY_DOUBLE,
    MODE_KEY_RAINBOW,
    MODE_KEY_TRIPLE,
    MODE_KEY_WHITE,
    MODE_NAMES_EN,
    MODE_NAMES_PL,
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


def _mode_names_for_language(language: str | None) -> dict[str, str]:
    """Return localized mode display names for HA's current language."""
    if (language or "en").lower().startswith("pl"):
        return MODE_NAMES_PL
    return MODE_NAMES_EN


def _mode_name_to_key(name: str, language: str | None) -> str | None:
    """Resolve a localized mode name back to its stable mode key."""
    names = _mode_names_for_language(language)
    for key, display in names.items():
        if display == name:
            return key

    # Also accept the other language and the stable key for automations.
    for name_map in (MODE_NAMES_EN, MODE_NAMES_PL):
        for key, display in name_map.items():
            if display == name:
                return key
    if name in MODE_NAMES_EN:
        return name
    return None


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


def _is_empty_color(value: Any) -> bool:
    """Return true for missing or black controller color values."""
    if not value:
        return True
    color = str(value).upper().replace("0X", "").replace("#", "")
    return color in ("", "000000")


def _zone_color(zone: dict[str, Any], key: str, fallback: str) -> str:
    """Return a color from zone state, falling back when it is empty/black."""
    value = zone.get(key)
    return fallback if _is_empty_color(value) else str(value)


def _zone_hue_to_elegant(value: Any, fallback: int = 0) -> int:
    """Convert a stored zone hue to the 0-255 scale used by set_zone."""
    try:
        hue = float(value)
    except (TypeError, ValueError):
        return fallback

    if hue > ELEGANT_HUE_MAX:
        hue = hue * ELEGANT_HUE_MAX / 360
    return max(0, min(ELEGANT_HUE_MAX - 1, int(hue)))


def _mode_switch_payload(mode_key: str, zone: dict[str, Any]) -> dict[str, Any]:
    """Build the protocol payload needed to switch light mode."""
    if mode_key == MODE_KEY_COLOR:
        try:
            current_sat = int(zone.get("color_saturation", 0) or 0)
        except (TypeError, ValueError):
            current_sat = 0
        return {
            "color_mode": COLOR_MODE_SINGLE,
            "color_saturation": current_sat if current_sat > 0 else 100,
        }

    if mode_key == MODE_KEY_WHITE:
        return {
            "color_mode": COLOR_MODE_SINGLE,
            "color_saturation": 0,
            "color_1": DEFAULT_COLOR,
            "color_2": DEFAULT_COLOR,
            "color_3": DEFAULT_COLOR,
            "color_1_hue": 0,
            "color_2_hue": 0,
            "color_3_hue": 0,
        }

    if mode_key == MODE_KEY_DOUBLE:
        return {
            "color_mode": COLOR_MODE_DOUBLE,
            "color_1": _zone_color(zone, "color_1", "0xFF0000"),
            "color_1_hue": _zone_hue_to_elegant(zone.get("color_1_hue"), 0),
            "color_2": _zone_color(zone, "color_2", "0x00FF00"),
            "color_2_hue": _zone_hue_to_elegant(zone.get("color_2_hue"), 85),
        }

    if mode_key == MODE_KEY_TRIPLE:
        return {
            "color_mode": COLOR_MODE_TRIPLE,
            "color_1": _zone_color(zone, "color_1", "0xFF0000"),
            "color_1_hue": _zone_hue_to_elegant(zone.get("color_1_hue"), 0),
            "color_2": _zone_color(zone, "color_2", "0x00FF00"),
            "color_2_hue": _zone_hue_to_elegant(zone.get("color_2_hue"), 85),
            "color_3": _zone_color(zone, "color_3", "0x0000FF"),
            "color_3_hue": _zone_hue_to_elegant(zone.get("color_3_hue"), 170),
        }

    if mode_key == MODE_KEY_RAINBOW:
        return {"color_mode": COLOR_MODE_RAINBOW}

    if mode_key == MODE_KEY_AUTO_CHANGE:
        return {"color_mode": COLOR_MODE_AUTO_CHANGE}

    raise ValueError(f"Unknown mode key: {mode_key}")


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


# Scenes bitfield: one bit per effect ID. Minimum 4 uint32 words (128 IDs)
# for backward compatibility with older controllers; extended up to
# SCENES_MAX_WORDS for controllers that define effects with higher IDs
# (e.g. type 80 has effects up to ID 182).
SCENES_MIN_WORDS = 4
SCENES_MAX_WORDS = 8      # supports effect IDs 0..255
SCENES_MAX_ID = SCENES_MAX_WORDS * 32 - 1


def _decode_scenes_to_effect_ids(scenes: list[int] | tuple[int, ...]) -> list[int]:
    """Decode a scenes bitfield into a sorted list of enabled effect IDs.

    Scans ALL elements of the incoming array (up to SCENES_MAX_WORDS),
    not only the first 4 — modern controllers may send extended arrays.
    """
    if not isinstance(scenes, (list, tuple)):
        return []

    effect_ids: list[int] = []
    for word_index, raw_word in enumerate(scenes[:SCENES_MAX_WORDS]):
        try:
            word = int(raw_word) & 0xFFFFFFFF
        except (TypeError, ValueError):
            continue

        for bit_index in range(32):
            if word & (1 << bit_index):
                effect_ids.append(word_index * 32 + bit_index)

    return effect_ids


# def _encode_effect_id_to_scenes(effect_id: int) -> list[int]:
#     """Encode a single effect_id into a scenes bitfield (4..8 uint32 words)."""
#     return _encode_effect_ids_to_scenes([effect_id])

def _encode_effect_id_to_scenes(effect_id: int) -> list[int]:
    """Encode single effect_id (0..127) into 4x uint32 bitfield."""
    if effect_id < 0 or effect_id > 127:
        raise ValueError(f"effect_id out of range: {effect_id}")

    scenes = [0, 0, 0, 0]
    word_idx = effect_id // 32
    bit_idx = effect_id % 32
    scenes[word_idx] = 1 << bit_idx
    return scenes

def _encode_effect_ids_to_scenes(effect_ids: list[int]) -> list[int]:
    """Encode a list of effect_ids into a scenes bitfield.

    Output array size is at least SCENES_MIN_WORDS, and grows up to
    SCENES_MAX_WORDS to accommodate higher-ID effects. Keeping the
    minimum preserves the on-wire shape for older controllers.
    """
    # Determine required size from the largest ID (default to minimum)
    max_id = max(effect_ids) if effect_ids else 0
    if max_id < 0 or max_id > SCENES_MAX_ID:
        raise ValueError(f"effect_id out of range: {max_id}")
    words_needed = max(SCENES_MIN_WORDS, max_id // 32 + 1)

    scenes = [0] * words_needed
    for effect_id in effect_ids:
        if effect_id < 0 or effect_id > SCENES_MAX_ID:
            raise ValueError(f"effect_id out of range: {effect_id}")
        scenes[effect_id // 32] |= 1 << (effect_id % 32)
    return scenes


class ElegantLight(CoordinatorEntity[ElegantCoordinator], LightEntity):
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
        try:
            self._zone_type = int(zone_data.get("type", 0))
        except (TypeError, ValueError):
            self._zone_type = 0
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
            available_modes = set(zone_data.get("available_modes") or [])
            supported_color_modes: set[ColorMode] = set()
            if MODE_KEY_COLOR in available_modes:
                supported_color_modes.add(ColorMode.HS)
            if MODE_KEY_WHITE in available_modes:
                supported_color_modes.add(ColorMode.COLOR_TEMP)
            self._attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
            self._attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN
            if not supported_color_modes:
                supported_color_modes.add(ColorMode.BRIGHTNESS)
            self._attr_supported_color_modes = supported_color_modes
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

        try:
            elegant_mode = int(self._zone.get("color_mode", 0))
        except (TypeError, ValueError):
            elegant_mode = 0
        try:
            saturation = int(self._zone.get("color_saturation", 0) or 0)
        except (TypeError, ValueError):
            saturation = 0
        supported = self.supported_color_modes or set()

        # If saturation is 0, it's white/color temp mode
        if (
            saturation == 0
            and elegant_mode == COLOR_MODE_SINGLE
            and ColorMode.COLOR_TEMP in supported
        ):
            return ColorMode.COLOR_TEMP
        if ColorMode.HS in supported:
            return ColorMode.HS
        if ColorMode.COLOR_TEMP in supported:
            return ColorMode.COLOR_TEMP
        if ColorMode.BRIGHTNESS in supported:
            return ColorMode.BRIGHTNESS
        return ColorMode.ONOFF

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

        effects_map: dict[int, str] = self._zone.get("available_effects", {})
        roll_map: dict[int, str] = self._zone.get("available_roll_effects", {})
        mode_names = self._mode_names()
        mode_key = self._zone_mode_key()

        # --- Regular (bitfield) effects — consumed by custom card ----------
        # Effect ID → name mapping for THIS zone (per controller type).
        # Keys become strings after JSON serialization — card handles both.
        attrs["effect_names"] = {
            str(k): v for k, v in effects_map.items()
        }

        # Active effect IDs (real IDs == bit positions in scenes)
        scenes = self._zone.get("scenes")
        if scenes:
            active_ids = _decode_scenes_to_effect_ids(scenes)
        else:
            active_ids = []
        attrs["active_effect_ids"] = active_ids

        # Back-compat: list of active effect NAMES (used by old card and
        # generic HA integrations). Falls back to "Effect <id>" when the
        # name is missing for a given ID in this zone's dictionary.
        active_names: list[str] = []
        for eid in sorted(active_ids):
            name = effects_map.get(eid) or effects_map.get(str(eid))
            active_names.append(name if name else f"Effect {eid}")
        attrs["active_effects"] = active_names

        # --- Active light mode ---------------------------------------------
        attrs[ATTR_ACTIVE_MODE_ID] = self._zone.get("color_mode", 0)
        attrs[ATTR_ACTIVE_MODE] = mode_names.get(mode_key) if mode_key else None
        attrs[ATTR_AVAILABLE_MODES] = [
            mode_names[key]
            for key in self._zone.get("available_modes", [])
            if key in mode_names
        ]
        attrs[ATTR_EFFECT_FLAGS] = {
            str(k): v for k, v in self._zone.get("effect_flags", {}).items()
        }

        if roll_map:
            attrs["roll_effect_names"] = {
                str(k): v for k, v in roll_map.items()
            }
            roll_id = self._zone.get("roll_effect")
            try:
                roll_id_int = int(roll_id)
            except (TypeError, ValueError):
                roll_id_int = None
            attrs["active_roll_effect_id"] = roll_id_int
            if roll_id_int is None:
                attrs["active_roll_effect"] = None
            else:
                attrs["active_roll_effect"] = (
                    roll_map.get(roll_id_int)
                    or roll_map.get(str(roll_id_int))
                )

        return attrs

    def _mode_names(self) -> dict[str, str]:
        """Return localized mode names for this entity."""
        hass = self.hass or self.coordinator.hass
        return _mode_names_for_language(hass.config.language)

    def _zone_mode_key(self) -> str | None:
        """Return the current stable light mode key for this zone."""
        try:
            mode = int(self._zone.get("color_mode", COLOR_MODE_SINGLE))
        except (TypeError, ValueError):
            mode = COLOR_MODE_SINGLE
        try:
            saturation = int(self._zone.get("color_saturation", 0) or 0)
        except (TypeError, ValueError):
            saturation = 0

        if mode == COLOR_MODE_SINGLE:
            return MODE_KEY_WHITE if saturation == 0 else MODE_KEY_COLOR
        return {
            COLOR_MODE_DOUBLE: MODE_KEY_DOUBLE,
            COLOR_MODE_RAINBOW: MODE_KEY_RAINBOW,
            COLOR_MODE_TRIPLE: MODE_KEY_TRIPLE,
            COLOR_MODE_AUTO_CHANGE: MODE_KEY_AUTO_CHANGE,
        }.get(mode)

    @property
    def supported_features(self) -> LightEntityFeature:
        features = LightEntityFeature(0)

        if self._zone_type != 0:
            features |= LightEntityFeature.TRANSITION

            if self._zone.get("available_effects"):
                features |= LightEntityFeature.EFFECT

        _LOGGER.debug("Supported features for zone_id=%d: %s", self._idx, features)

        return features

    @property
    def effect_list(self) -> list[str]:
        effects_map: dict[int, str] = self._zone.get("available_effects", {})
        return list(effects_map.values())

    @property
    def effect(self) -> str | None:
        effects_map: dict[int, str] = self._zone.get("available_effects", {})
        scenes = self._zone.get("scenes")
        if not scenes:
            return None

        enabled_effect_ids = _decode_scenes_to_effect_ids(scenes)
        if not enabled_effect_ids:
            return None

        names = []
        for eid in sorted(enabled_effect_ids):
            name = effects_map.get(eid) or effects_map.get(str(eid))
            if name:
                names.append(name)

        return ", ".join(names) if names else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on with optional parameters."""
        params: dict[str, Any] = {}

        if ATTR_EFFECT in kwargs:
            selected_name = kwargs[ATTR_EFFECT]

            effects_map = self._zone.get("available_effects", {})
            effect_id = next(
                (
                    int(eid)
                    for eid, name in effects_map.items()
                    if name == selected_name
                ),
                None,
            )

            if effect_id is None:
                _LOGGER.warning(
                    "Effect not found in zone %d: %s",
                    self._idx, selected_name,
                )
            else:
                params["scenes"] = _encode_effect_id_to_scenes(effect_id)

        if ATTR_BRIGHTNESS in kwargs:
            # HA 0-255 -> Elegant 0-100
            ha_bright = kwargs[ATTR_BRIGHTNESS]
            params["bright"] = max(1, int(ha_bright * 100 / 255))

        if ATTR_HS_COLOR in kwargs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            r, g, b = _hs_to_rgb(hue, saturation)
            # Elegant SEND uses hue in 0-255 scale: elegant_hue = ha_hue * 256 / 360
            elegant_hue = int(hue * ELEGANT_HUE_MAX / 360)
            params["color_1"] = _rgb_to_hex_0x(r, g, b)
            params["color_1_hue"] = elegant_hue
            params["color_saturation"] = max(1, int(saturation))
            params["color_mode"] = COLOR_MODE_SINGLE

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            elegant_temp = _kelvin_to_elegant_temp(kelvin)
            params["white_temperature"] = elegant_temp
            # Switch to white mode: color_mode 0 with saturation 0
            params["color_saturation"] = 0
            params["color_mode"] = COLOR_MODE_SINGLE
            params["color_1"] = DEFAULT_COLOR
            params["color_1_hue"] = 0
            params["color_2"] = DEFAULT_COLOR
            params["color_2_hue"] = 0
            params["color_3"] = DEFAULT_COLOR
            params["color_3_hue"] = 0

        # Always ensure the light is turned on
        params["is_on"] = True

        await self.coordinator.async_set_zone(self._idx, **params)

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

    async def async_set_effects(self, effect_names: list[str]) -> None:
        """Set multiple effects on this zone by their names."""
        effects_map: dict[int, str] = self._zone.get("available_effects", {})

        effect_ids: list[int] = []
        for name in effect_names:
            eid = next(
                (int(k) for k, v in effects_map.items() if v == name),
                None,
            )
            if eid is None:
                _LOGGER.warning("Effect not found: %s (zone %d)", name, self._idx)
            else:
                effect_ids.append(eid)

        if not effect_ids:
            _LOGGER.warning("No valid effects to set for zone %d", self._idx)
            return

        scenes = _encode_effect_ids_to_scenes(effect_ids)
        _LOGGER.debug(
            "Setting %d effects for zone %d: %s -> scenes=%s",
            len(effect_ids), self._idx, effect_names, scenes,
        )
        await self.coordinator.async_set_zone(self._idx, scenes=scenes)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()






