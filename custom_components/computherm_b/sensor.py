"""Computherm sensors: unchanged relay plus four distinct sensor devices."""
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


def _safe(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _sensor_id(key: str, info: dict) -> str:
    for field in ("physical_sensor_id", "sensor_id", "channel", "id"):
        if info.get(field) not in (None, ""):
            return str(info[field])
    match = re.search(r"(?:^|_)(\d+)(?:_|$)", key)
    return match.group(1) if match else str(info.get("sensor", key))


def _sensor_name(sid: str, info: dict) -> str:
    name = str(info.get("name") or "").strip()
    return name or {"1": "Sensore principale", "2": "Sensore 2", "3": "Sensore 3", "4": "Sensore 4"}.get(sid, f"Sensore {sid}")


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    await coordinator.async_config_entry_first_refresh()
    existing: set[str] = set()

    @callback
    def add_for_device(serial: str) -> None:
        if not coordinator.devices_with_base_info.get(serial):
            return
        readings = coordinator.device_data.get(serial, {}).get(DA.SENSOR_READINGS, {}) or {}
        entities = []
        relay_key = f"{serial}:relay"
        if relay_key not in existing:
            existing.add(relay_key)
            entities.append(ComputhermRelaySensor(coordinator, serial))
        for key, info in readings.items():
            kind = str(info.get("type") or "").upper()
            if kind not in ("TEMPERATURE", "HUMIDITY"):
                continue
            tracking = f"{serial}:{key}:{kind}"
            if tracking not in existing:
                existing.add(tracking)
                entities.append(ComputhermReadingSensor(coordinator, serial, key, info, kind))
            if kind == "TEMPERATURE" and "battery" in info:
                tracking = f"{serial}:{key}:BATTERY"
                if tracking not in existing:
                    existing.add(tracking)
                    entities.append(ComputhermBatterySensor(coordinator, serial, key, info))
        if entities:
            async_add_entities(entities, True)

    for serial in coordinator.devices:
        add_for_device(serial)
    @callback
    def update() -> None:
        for serial in coordinator.devices:
            add_for_device(serial)
    config_entry.async_on_unload(coordinator.async_add_listener(update))


class Base(CoordinatorEntity):
    _attr_has_entity_name = True
    def __init__(self, coordinator, serial):
        super().__init__(coordinator); self.serial = serial
    @property
    def device_data(self):
        return self.coordinator.device_data.get(self.serial, {})
    @property
    def available(self):
        return bool(self.device_data.get(DA.ONLINE, False))


class ComputhermRelaySensor(Base, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_translation_key = "relay"
    def __init__(self, coordinator, serial):
        super().__init__(coordinator, serial)
        self._attr_unique_id = f"{DOMAIN}_{serial}_relay"
        d = coordinator.devices[serial]
        self._attr_device_info = {"identifiers": {(DOMAIN, serial)}, "serial_number": serial,
            "name": f"Computherm {serial}", "manufacturer": "Computherm",
            "model": d.get(DA.DEVICE_TYPE, "") or "B Series Thermostat",
            "sw_version": d.get(DA.FW_VERSION), "hw_version": d.get("type")}
    @property
    def is_on(self):
        return self.device_data.get(DA.RELAY_STATE)
    @property
    def icon(self):
        return "mdi:electric-switch-closed" if self.is_on else "mdi:electric-switch"


class RFBase(Base):
    def __init__(self, coordinator, serial, key, info):
        self.sensor_key = key; self.sensor_id = _sensor_id(key, info)
        super().__init__(coordinator, serial)
        # Exactly one device per physical id. No via_device deprecation.
        self._attr_device_info = {"identifiers": {(DOMAIN, f"{serial}_rf_sensor_{self.sensor_id}")},
            "name": _sensor_name(self.sensor_id, info), "manufacturer": "ProSmart / Computherm",
            "model": "BBoil RF sensor"}
    @property
    def reading(self):
        return self.device_data.get(DA.SENSOR_READINGS, {}).get(self.sensor_key, {})


class ComputhermReadingSensor(RFBase, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT
    def __init__(self, coordinator, serial, key, info, kind):
        super().__init__(coordinator, serial, key, info)
        suffix = "temperature" if kind == "TEMPERATURE" else "humidity"
        # Full sensor_key prevents RELAY_1 / REMOTE_1 collisions without splitting devices.
        self._attr_unique_id = f"{DOMAIN}_{serial}_{_safe(key)}_{suffix}"
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
    def native_value(self):
        try: return float(self.reading.get("reading"))
        except (TypeError, ValueError): return None
    @property
    def available(self):
        return super().available and self.native_value is not None
    @property
    def extra_state_attributes(self):
        return {"sensor_key": self.sensor_key, "sensor_id": self.sensor_id, "source": self.reading.get("src")}


class ComputhermBatterySensor(RFBase, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:battery"; _attr_name = "Batteria"
    def __init__(self, coordinator, serial, key, info):
        super().__init__(coordinator, serial, key, info)
        self._attr_unique_id = f"{DOMAIN}_{serial}_{_safe(key)}_battery"
    @property
    def native_value(self):
        value = self.reading.get("battery")
        try: return float(str(value).replace("%", "").strip()) if value is not None else None
        except (TypeError, ValueError): return None
    @property
    def available(self):
        return super().available and self.native_value is not None
