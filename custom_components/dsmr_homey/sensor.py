"""Sensor platform – raw OBIS + calculated analytics + health."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant, callback, CALLBACK_TYPE
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import StateType
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .coordinator import DSMRCoordinator

_LOGGER = logging.getLogger(__name__)

# ----------------------------------------------------------------------
#  Helper – calculated sensor (no coordinator)
# ----------------------------------------------------------------------
class CalculatedSensor(SensorEntity):
    _attr_should_poll = False
    _attr_available = True  # ← always available

    def __init__(
            self,
            hass: HomeAssistant,
            unique_id: str,
            name: str,
            icon: str,
            unit: str | None,
            device_class: str | None,
            state_class: str | None,
            calc_func: Callable[[], float | int | None],
    ):
        self.hass = hass
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._calc = calc_func
        self._attr_native_value = None
        self._remove_tracker: CALLBACK_TYPE | None = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "homey_dsmr")},
            name="Homey DSMR",
            manufacturer="Homey",
            model="Energy Dongle",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to all source sensors."""
        # Extract free variable names from the lambda/function
        free_vars = getattr(self._calc, "__code__", None)
        if not free_vars:
            return
        source_keys = free_vars.co_freevars
        entity_ids = [f"sensor.dsmr_{key}" for key in source_keys]

        if entity_ids:
            self._remove_tracker = async_track_state_change_event(
                self.hass, entity_ids, self._handle_state_change
            )
        await self._update_value()

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_tracker:
            self._remove_tracker()

    @callback
    def _handle_state_change(self, _event=None):
        self._update_value()

    async def _update_value(self):
        try:
            new = self._calc()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Calc %s failed: %s", self.entity_id, exc)
            new = None
        if new != self._attr_native_value:
            self._attr_native_value = new
            self.async_write_ha_state()
            _LOGGER.debug(
                "CALC %s → %s %s",
                self.entity_id,
                new,
                self._attr_native_unit_of_measurement or "",
                )

# ----------------------------------------------------------------------
#  Raw OBIS sensor
# ----------------------------------------------------------------------
class DSMRSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, coordinator: DSMRCoordinator, obis_key: str):
        self.coordinator = coordinator
        self._obis_key = obis_key

        schema = coordinator._schema.get(obis_key, {})
        desc = schema.get("description", obis_key)
        self._attr_name = f"DSMR {desc}"
        self._attr_unique_id = f"dsmr_homey_{obis_key}"
        self._attr_native_unit_of_measurement = schema.get("unit")
        self._attr_device_class = schema.get("device_class")
        self._attr_state_class = schema.get("state_class")
        self._attr_icon = schema.get("icon")
        self._last_value: Any = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "homey_dsmr")},
            name="Homey DSMR",
            manufacturer="Homey",
            model="Energy Dongle",
        )

        _LOGGER.info(
            "SENSOR CREATED → %s | uid=%s | class=%s | icon=%s",
            self._attr_name, self._attr_unique_id,
            self._attr_device_class, self._attr_icon,
        )

    @property
    def available(self) -> bool:
        return True  # never unavailable

    @property
    def native_value(self) -> StateType:
        val = self.coordinator.get_data().get(self._obis_key, self._last_value)
        if val != self._last_value:
            _LOGGER.debug(
                "SENSOR UPDATE %s → %s %s",
                self.entity_id,
                val,
                self._attr_native_unit_of_measurement or "",
                )
            self._last_value = val
        return val

    @property
    def extra_state_attributes(self) -> dict | None:
        return {"obis_code": self._obis_key}

    async def async_added_to_hass(self) -> None:
        self.coordinator.async_add_listener(self._handle_coordinator_update)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.async_remove_listener(self._handle_coordinator_update)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

# ----------------------------------------------------------------------
#  Connection health sensor
# ----------------------------------------------------------------------
class ConnectionHealthSensor(SensorEntity):
    _attr_should_poll = False
    _attr_name = "DSMR Connection Health"
    _attr_unique_id = "dsmr_homey_connection_health"
    _attr_icon = "mdi:lan-connect"
    _attr_device_info = DeviceInfo(
        identifiers={(DOMAIN, "homey_dsmr")},
        name="Homey DSMR",
        manufacturer="Homey",
        model="Energy Dongle",
    )

    def __init__(self, coordinator: DSMRCoordinator):
        self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return self.coordinator.available

    @property
    def native_value(self) -> str:
        return "online" if self.coordinator.available else "offline"

    @property
    def extra_state_attributes(self) -> dict:
        last_ts = self.coordinator._last_telegram
        seconds = int(time.time() - last_ts) if last_ts else None
        return {"last_telegram_seconds_ago": seconds}

    async def async_added_to_hass(self) -> None:
        self.coordinator.async_add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.async_remove_listener(self._handle_update)

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
        seconds = self.extra_state_attributes["last_telegram_seconds_ago"]
        if seconds is not None:
            _LOGGER.debug("HEALTH → %s (last telegram %d s ago)", self.native_value, seconds)
        else:
            _LOGGER.debug("HEALTH → %s (no telegram yet)", self.native_value)

# ----------------------------------------------------------------------
#  Platform setup
# ----------------------------------------------------------------------
async def async_setup_entry(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    coordinator: DSMRCoordinator = hass.data[DOMAIN][entry.entry_id]

    # ---- 1. Raw OBIS sensors ------------------------------------------------
    existing: set[str] = set()
    first_batch = True

    def _make_raw():
        nonlocal first_batch
        data = coordinator.get_data()
        if not data:
            _LOGGER.debug("No data – skip raw creation")
            return []
        cur = set(data.keys())
        to_create = cur if first_batch else cur - existing
        entities = [DSMRSensor(coordinator, k) for k in to_create]
        existing.update(to_create)
        if first_batch and entities:
            first_batch = False
            _LOGGER.info("FIRST RAW BATCH – %d sensors", len(entities))
        elif entities:
            _LOGGER.info("ADDED %d new raw sensors", len(entities))
        return entities

    async_add_entities(_make_raw(), True)

    @callback
    def _on_coordinator_update():
        new = _make_raw()
        if new:
            async_add_entities(new, True)

    coordinator.async_add_listener(_on_coordinator_update)

    # ---- 2. Helper to read float ------------------------------------------------
    def _f(key: str) -> float:
        return float(coordinator.get_data().get(key, 0))

    # ---- 3. Calculated analytics ------------------------------------------------
    calc_entities = []

    # Net active power
    calc_entities.append(
        CalculatedSensor(
            hass,
            "dsmr_homey_net_power",
            "DSMR Net Power",
            "mdi:flash",
            "kW",
            SensorDeviceClass.POWER,
            SensorStateClass.MEASUREMENT,
            lambda: _f("instantaneous_active_power_import")
                    - _f("instantaneous_active_power_export"),
        )
    )

    # Total reactive power (P+ + P-)
    calc_entities.append(
        CalculatedSensor(
            hass,
            "dsmr_homey_total_reactive",
            "DSMR Total Reactive Power",
            "mdi:sine-wave",
            "kVAr",
            None,
            SensorStateClass.MEASUREMENT,
            lambda: _f("positive_reactive_instantaneous_power")
                    + _f("negative_reactive_instantaneous_power"),
        )
    )

    # Net reactive power (P+ - P-)
    calc_entities.append(
        CalculatedSensor(
            hass,
            "dsmr_homey_net_reactive",
            "DSMR Net Reactive Power",
            "mdi:sine-wave",
            "kVAr",
            None,
            SensorStateClass.MEASUREMENT,
            lambda: _f("positive_reactive_instantaneous_power")
                    - _f("negative_reactive_instantaneous_power"),
        )
    )

    # Phase balance deviation
    def phase_balance():
        p1 = _f("l1_active_power_import")
        p2 = _f("l2_active_power_import")
        p3 = _f("l3_active_power_import")
        if p1 == p2 == p3 == 0:
            return 0.0
        avg = (p1 + p2 + p3) / 3
        return max(abs(p1 - avg), abs(p2 - avg), abs(p3 - avg))

    calc_entities.append(
        CalculatedSensor(
            hass,
            "dsmr_homey_phase_balance",
            "DSMR Phase Balance Deviation",
            "mdi:balance",
            "kW",
            SensorDeviceClass.POWER,
            SensorStateClass.MEASUREMENT,
            phase_balance,
        )
    )

    # Power factor (cos φ) – approximation
    def power_factor():
        active = _f("instantaneous_active_power_import")
        reactive = abs(_f("positive_reactive_instantaneous_power"))
        apparent = (active**2 + reactive**2) ** 0.5
        return round(active / apparent, 3) if apparent > 0 else 1.0

    calc_entities.append(
        CalculatedSensor(
            hass,
            "dsmr_homey_power_factor",
            "DSMR Power Factor",
            "mdi:cosine-wave",
            None,
            None,
            SensorStateClass.MEASUREMENT,
            power_factor,
        )
    )

    # TODO: introduce dynamic pricing
    # Estimated cost
    COST_PER_KWH = 1.5  # Change in your config if needed

    def daily_cost():
        import_today = _f("active_energy_import_(total)") - coordinator.get_data().get("_import_yesterday", 0)
        return round(import_today * COST_PER_KWH, 2)

    # Store yesterday's import at midnight (via automation or use recorder)
    # For now, we expose the function – HA will call it on every update
    calc_entities.append(
        CalculatedSensor(
            hass,
            "dsmr_homey_daily_cost",
            "DSMR Daily Cost",
            "mdi:currency-eur",
            "SEK",
            None,
            None,
            daily_cost,
        )
    )

    # Peak power today (resets at midnight)
    def peak_power():
        current = _f("instantaneous_active_power_import")
        stored = coordinator.get_data().get("_peak_today", 0)
        return max(current, stored)

    calc_entities.append(
        CalculatedSensor(
            hass,
            "dsmr_homey_peak_power",
            "DSMR Peak Power Today",
            "mdi:weather-lightning",
            "kW",
            SensorDeviceClass.POWER,
            SensorStateClass.MEASUREMENT,
            peak_power,
        )
    )

    async_add_entities(calc_entities, True)
    _LOGGER.info("Created %d calculated analytics sensors", len(calc_entities))

    # ---- 4. Health sensor ---------------------------------------------------
    health = ConnectionHealthSensor(coordinator)
    async_add_entities([health], True)
    _LOGGER.info("Created connection health sensor")
