"""Sensor platform for Computherm B with separate physical RF devices."""
from __future__ import annotations

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


def _safe(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")


def _physical_id(sensor_key: str, info: dict) -> str:
    for field in ("physical_sensor_id", "sensor_id", "channel", "id"):
        if info.get(field) not in (None, ""):
            return str(info[field])
    parts = sensor_key.split("_")
    if len(parts) > 1 and parts[1].isdigit():
        return parts[1]
    return str(info.get("sensor", sensor_key))


def _device_key(sensor_key: str, info: dict) -> str:
    src = str(info.get("src") or sensor_key.split("_", 1)[0]).lower()
    return f"{src}_{_physical_id(sensor_key, info)}"


def _device_name(sensor_key: str, info: dict) -> str:
    name = str(info.get("name") or "").strip()
    if name:
        return name
    sid = _physical_id(sensor_key, info)
    return {"1": "Sensore principale", "2": "Sensore 2", "3": "Sensore 3", "4": "Sensore 4"}.get(sid, f"Sensore {sid}")


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ComputhermDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    await coordinator.async_config_entry_first_refresh()
    existing: set[str] = set()

    @callback
    def add_entities(serial: str) -> None:
        if not coordinator.devices_with_base_info.get(serial):
            return
        data = coordinator.device_data.get(serial, {})
        readings = data.get(DA.SENSOR_READINGS, {}) or {}
        new: list = []

        uid = f"{serial}:relay"
        if uid not in existing:
            new.append(ComputhermRelaySensor(coordinator, serial))
            existing.add(uid)

        for sensor_key, info in readings.items():
            kind = str(info.get("type") or "").upper()
            if kind not in ("TEMPERATURE", "HUMIDITY"):
                continue
            tracking = f"{serial}:{sensor_key}:{kind}"
            if tracking not in existing:
                new.append(ComputhermReadingSensor(coordinator, serial, sensor_key, info, kind))
                existing.add(tracking)
            if kind == "TEMPERATURE" and "battery" in info:
                tracking = f"{serial}:{sensor_key}:BATTERY"
                if tracking not in existing:
                    new.append(ComputhermBatterySensor(coordinator, serial, sensor_key, info))
                    existing.add(tracking)
        if new:
            async_add_entities(new, True)

    for serial in coordinator.devices:
        add_entities(serial)

    @callback
    def update() -> None:
        for serial in coordinator.devices:
            add_entities(serial)

    config_entry.async_on_unload(coordinator.async_add_listener(update))


class Base(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, serial: str) -> None:
        super().__init__(coordinator)
        self.serial = serial

    @property
    def device_data(self) -> dict[str, Any]:
        return self.coordinator.device_data.get(self.serial, {})

    @property
    def available(self) -> bool:
        return bool(self.device_data.get(DA.ONLINE, False))


class ComputhermRelaySensor(Base, BinarySensorEntity):
    """Original relay presentation and unique id."""
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "relay"

    def __init__(self, coordinator, serial: str) -> None:
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
        return self.device_data.get(DA.RELAY_STATE)

    @property
    def icon(self) -> str:
        return "mdi:electric-switch-closed" if self.is_on else "mdi:electric-switch"


class RFBase(Base):
    def __init__(self, coordinator, serial: str, sensor_key: str, info: dict) -> None:
        self.sensor_key = sensor_key
        self.physical_id = _physical_id(sensor_key, info)
        self.rf_device_key = _device_key(sensor_key, info)
        super().__init__(coordinator, serial)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{serial}_{self.rf_device_key}")},
            "name": _device_name(sensor_key, info),
            "manufacturer": "ProSmart / Computherm",
            "model": "BBoil RF sensor",
        }

    @property
    def reading(self) -> dict:
        return self.device_data.get(DA.SENSOR_READINGS, {}).get(self.sensor_key, {})


class ComputhermReadingSensor(RFBase, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, serial, sensor_key, info, kind) -> None:
        self.kind = kind
        super().__init__(coordinator, serial, sensor_key, info)
        suffix = "temperature" if kind == "TEMPERATURE" else "humidity"
        # sensor_key is included so two sources can never get the same unique id.
        self._attr_unique_id = f"{DOMAIN}_{serial}_{_safe(sensor_key)}_{suffix}"
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
        return {"sensor_key": self.sensor_key, "sensor_id": self.physical_id,
                "source": self.reading.get("src")}


class ComputhermBatterySensor(RFBase, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:battery"
    _attr_name = "Batteria"

    def __init__(self, coordinator, serial, sensor_key, info) -> None:
        super().__init__(coordinator, serial, sensor_key, info)
        self._attr_unique_id = f"{DOMAIN}_{serial}_{_safe(sensor_key)}_battery"

    @property
    def native_value(self) -> float | None:
        value = self.reading.get("battery")
        try:
            return float(str(value).replace("%", "").strip()) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None
