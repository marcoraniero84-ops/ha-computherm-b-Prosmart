"""Sensor platform for Computherm B integration.

Multi-RF-sensor variant: each physical sensor is exposed as a separate
Home Assistant device. The relay remains attached to the parent device.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COORDINATOR, DOMAIN
from .const import DeviceAttributes as DA
from .coordinator import ComputhermDataUpdateCoordinator

_LOGGER = logging.getLogger(__package__)


def _normal(value: Any) -> str:
    """Return a stable comparison string."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _sensor_id(device_data: dict, sensor_key: str, sensor_info: dict) -> str:
    """Resolve the real physical sensor id without hard-coding sensor_key.

    Priority:
    1. explicit id/channel fields already present in the WebSocket reading;
    2. the matching record returned by /sensors;
    3. the numeric part of sensor_key;
    4. sensor field, then sensor_key itself.
    """
    for field in ("physical_sensor_id", "sensor_id", "channel", "id"):
        value = sensor_info.get(field)
        if value not in (None, ""):
            return str(value)

    src = _normal(sensor_info.get("src"))
    kind = _normal(sensor_info.get("type"))
    name = _normal(sensor_info.get("name"))
    metadata = device_data.get("sensor_metadata", []) or []

    candidates = []
    for item in metadata:
        if src and _normal(item.get("src")) != src:
            continue
        if kind and _normal(item.get("type")) != kind:
            continue
        item_name = _normal(item.get("name"))
        score = 2 if name and item_name == name else 1
        candidates.append((score, item))
    if candidates:
        candidates.sort(key=lambda value: value[0], reverse=True)
        item = candidates[0][1]
        for field in ("physical_sensor_id", "sensor_id", "channel", "id", "sensor"):
            value = item.get(field)
            if value not in (None, ""):
                return str(value)

    match = re.search(r"(?:^|_)(\d+)$", sensor_key)
    if match:
        return match.group(1)
    value = sensor_info.get("sensor")
    return str(value if value not in (None, "") else sensor_key)


def _sensor_name(sensor_id: str, sensor_info: dict) -> str:
    """Prefer cloud name, with stable Italian fallbacks for ids 1..4."""
    cloud_name = str(sensor_info.get("name") or "").strip()
    if cloud_name:
        return cloud_name
    return {
        "1": "Sensore principale",
        "2": "Camera",
        "3": "Cucina",
        "4": "Cameretta",
    }.get(sensor_id, f"Sensore {sensor_id}")


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Computherm sensors."""
    coordinator: ComputhermDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    await coordinator.async_config_entry_first_refresh()
    existing: set[str] = set()

    @callback
    def add_for_device(serial: str) -> None:
        if not coordinator.devices_with_base_info.get(serial):
            return
        data = coordinator.device_data.get(serial, {})
        readings = data.get(DA.SENSOR_READINGS, {}) or {}
        entities: list = []

        relay_uid = f"{serial}:relay"
        if relay_uid not in existing:
            entities.append(ComputhermRelaySensor(coordinator, serial))
            existing.add(relay_uid)

        for key, info in readings.items():
            kind = str(info.get("type") or "").upper()
            if kind not in ("TEMPERATURE", "HUMIDITY"):
                continue
            sid = _sensor_id(data, str(key), info)
            name = _sensor_name(sid, info)
            uid = f"{serial}:{sid}:{kind.lower()}:{key}"
            if uid not in existing:
                entities.append(ComputhermReadingSensor(coordinator, serial, str(key), sid, name, kind))
                existing.add(uid)

            if kind == "TEMPERATURE" and "battery" in info:
                battery_uid = f"{serial}:{sid}:battery:{key}"
                if battery_uid not in existing:
                    entities.append(ComputhermBatterySensor(coordinator, serial, str(key), sid, name))
                    existing.add(battery_uid)

        if entities:
            async_add_entities(entities, True)

    for serial in coordinator.devices:
        add_for_device(serial)

    @callback
    def handle_update() -> None:
        for serial in coordinator.devices:
            add_for_device(serial)

    config_entry.async_on_unload(coordinator.async_add_listener(handle_update))


class ComputhermEntityBase(CoordinatorEntity):
    """Common parent-device handling."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComputhermDataUpdateCoordinator, serial: str) -> None:
        super().__init__(coordinator)
        self.serial = serial

    @property
    def device_data(self) -> dict[str, Any]:
        return self.coordinator.device_data.get(self.serial, {})

    @property
    def available(self) -> bool:
        return bool(self.device_data.get(DA.ONLINE, False))


class ComputhermRelaySensor(ComputhermEntityBase, BinarySensorEntity):
    """Existing relay entity, kept on the parent Computherm device."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_name = "Relè"

    def __init__(self, coordinator: ComputhermDataUpdateCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{DOMAIN}_{serial}_relay"
        device = coordinator.devices[serial]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, serial)},
            "serial_number": serial,
            "name": f"Computherm {serial}",
            "manufacturer": "Computherm",
            "model": device.get(DA.DEVICE_TYPE, "") or "B Series Thermostat",
            "sw_version": device.get(DA.FW_VERSION),
            "hw_version": device.get("type"),
        }

    @property
    def is_on(self) -> bool | None:
        state = self.device_data.get(DA.RELAY_STATE)
        return state if state is not None else None

    @property
    def icon(self) -> str:
        return "mdi:electric-switch-closed" if self.is_on else "mdi:electric-switch"


class ComputhermRFBase(ComputhermEntityBase):
    """Base for entities belonging to an individual RF sensor device."""

    def __init__(
        self,
        coordinator: ComputhermDataUpdateCoordinator,
        serial: str,
        sensor_key: str,
        sensor_id: str,
        sensor_name: str,
    ) -> None:
        self.sensor_key = sensor_key
        self.sensor_id = sensor_id
        self.sensor_name = sensor_name
        super().__init__(coordinator, serial)
        self._attr_device_info = {
"identifiers": {(DOMAIN, f"{serial}_rf_sensor_{sensor_id}")},
"name": sensor_name,
"manufacturer": "ProSmart / Computherm",
"model": "BBoil RF sensor",
}

    @property
    def reading(self) -> dict:
        return self.device_data.get(DA.SENSOR_READINGS, {}).get(self.sensor_key, {})


class ComputhermReadingSensor(ComputhermRFBase, SensorEntity):
    """Temperature or humidity from one physical sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, serial, sensor_key, sensor_id, sensor_name, kind) -> None:
        self.kind = kind
        super().__init__(coordinator, serial, sensor_key, sensor_id, sensor_name)
        suffix = "temperature" if kind == "TEMPERATURE" else "humidity"
        safe_key = re.sub(
            r"[^a-zA-Z0-9_]",
            "_",
            str(sensor_key),
        )
        self._attr_unique_id = f"{DOMAIN}_{serial}_{safe_key}_{suffix}"
        self._attr_name = "Temperatura" if kind == "TEMPERATURE" else "Umidità"
        if kind == "TEMPERATURE":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_icon = "mdi:thermometer"
        else:
            self._attr_device_class = SensorDeviceClass.HUMIDITY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_icon = "mdi:water-percent"

    @property
    def native_value(self) -> float | None:
        value = self.reading.get("reading")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"sensor_key": self.sensor_key, "sensor_id": self.sensor_id, "source": self.reading.get("src")}


class ComputhermBatterySensor(ComputhermRFBase, SensorEntity):
    """Battery level for one physical sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:battery"
    _attr_name = "Batteria"

    def __init__(self, coordinator, serial, sensor_key, sensor_id, sensor_name) -> None:
        super().__init__(coordinator, serial, sensor_key, sensor_id, sensor_name)
        safe_key = re.sub(
            r"[^a-zA-Z0-9_]",
            "_",
            str(sensor_key),
        )
        self._attr_unique_id = f"{DOMAIN}_{serial}_{safe_key}_battery"

    @property
    def native_value(self) -> float | None:
        value = self.reading.get("battery")
        if value is None:
            return None
        try:
            return float(str(value).replace("%", "").strip())
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None
