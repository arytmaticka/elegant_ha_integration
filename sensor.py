"""Sensor platform for Elegant LED Controller integration."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ElegantCoordinator

_LOGGER = logging.getLogger(__name__)


def _hub_device_info(coordinator: ElegantCoordinator) -> DeviceInfo:
    """Return DeviceInfo for the main controller hub."""
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.mac)},
        name=f"Elegant LED {coordinator.user_settings.get('sn', '')}",
        manufacturer="Elegant",
        model="LED Controller",
        configuration_url=f"http://{coordinator.host}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Elegant diagnostic sensors from a config entry."""
    coordinator: ElegantCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        ElegantWifiRssiSensor(coordinator, entry),
        ElegantLastSeenSensor(coordinator, entry),
        ElegantClientsSensor(coordinator, entry),
        ElegantRemotesSensor(coordinator, entry),
        ElegantConnectSsidSensor(coordinator, entry),
        ElegantConnectStaSensor(coordinator, entry),
        ElegantControllerTimeSensor(coordinator, entry),
        ElegantSunriseSensor(coordinator, entry),
        ElegantSunsetSensor(coordinator, entry),
        ElegantActiveEffectSensor(coordinator, entry),
    ])


class _ElegantDiagnosticSensor(CoordinatorEntity[ElegantCoordinator], SensorEntity):
    """Base class for Elegant diagnostic sensors."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = _hub_device_info(coordinator)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def _interval(self, key: str, default=None):
        """Get a value from interval_data."""
        return self.coordinator.interval_data.get(key, default)


class ElegantWifiRssiSensor(_ElegantDiagnosticSensor):
    """WiFi RSSI signal strength sensor."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_wifi_rssi"
        self._attr_name = "WiFi RSSI"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.wifi_rssi


class ElegantLastSeenSensor(_ElegantDiagnosticSensor):
    """Last communication timestamp sensor (HA local clock)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_last_seen"
        self._attr_name = "Last seen"

    @property
    def native_value(self) -> datetime | None:
        ts = self.coordinator.last_seen
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return None


class ElegantClientsSensor(_ElegantDiagnosticSensor):
    """Number of connected WebSocket clients."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:devices"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_clients"
        self._attr_name = "Connected clients"

    @property
    def native_value(self) -> int | None:
        return self._interval("clients")


class ElegantRemotesSensor(_ElegantDiagnosticSensor):
    """Number of connected remotes (RF)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:remote"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_remotes"
        self._attr_name = "Connected remotes"

    @property
    def native_value(self) -> int | None:
        return self._interval("remotes")


class ElegantConnectSsidSensor(_ElegantDiagnosticSensor):
    """Connected WiFi network name."""

    _attr_icon = "mdi:wifi-settings"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_connect_ssid"
        self._attr_name = "WiFi SSID"

    @property
    def native_value(self) -> str | None:
        return self._interval("connect_ssid")


class ElegantConnectStaSensor(_ElegantDiagnosticSensor):
    """Whether the controller is connected to a WiFi network (STA mode)."""

    _attr_icon = "mdi:wifi-check"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_connect_sta"
        self._attr_name = "WiFi connected"

    @property
    def native_value(self) -> str | None:
        val = self._interval("connect_sta")
        if val is not None:
            return "Connected" if val else "Disconnected"
        return None


class ElegantControllerTimeSensor(_ElegantDiagnosticSensor):
    """Current date/time on the controller."""

    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_controller_time"
        self._attr_name = "Controller time"

    @property
    def native_value(self) -> str | None:
        date = self._interval("date")
        time_str = self._interval("time")
        if date and time_str:
            return f"{date} {time_str}"
        return None


class ElegantSunriseSensor(_ElegantDiagnosticSensor):
    """Sunrise time from controller."""

    _attr_icon = "mdi:weather-sunset-up"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_sunrise"
        self._attr_name = "Sunrise"

    @property
    def native_value(self) -> str | None:
        return self._interval("sunrise")


class ElegantSunsetSensor(_ElegantDiagnosticSensor):
    """Sunset time from controller."""

    _attr_icon = "mdi:weather-sunset-down"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_sunset"
        self._attr_name = "Sunset"

    @property
    def native_value(self) -> str | None:
        return self._interval("sunset")


class ElegantActiveEffectSensor(_ElegantDiagnosticSensor):
    """Currently active effect number."""

    _attr_icon = "mdi:auto-fix"

    def __init__(self, coordinator: ElegantCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.mac}_effect_now"
        self._attr_name = "Active effect"

    @property
    def native_value(self) -> int | None:
        return self._interval("effect_now")
