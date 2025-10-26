"""Sensor platform: creates entities from coordinator data."""

from __future__ import annotations
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from .const import DOMAIN
from .coordinator import DSMRCoordinator
import logging
from homeassistant.helpers.typing import StateType

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up sensor entities based on coordinator data keys."""
    coordinator: DSMRCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Cache for existing keys (global per entry to avoid recreating)
    existing_keys: set[str] = set()

    def _create_entities():
        data = coordinator.get_data()
        entities = []
        new_keys = set(data.keys()) - existing_keys
        for key in new_keys:
            ent = DSMRSensor(coordinator, key)
            existing_keys.add(key)
            entities.append(ent)
            _LOGGER.debug("Created new sensor for key: %s", key)
        return entities

    # initial entities
    new_entities = _create_entities()
    if new_entities:
        async_add_entities(new_entities, update_before_add=True)

    # Subscribe to coordinator updates: on change, create missing entities only
    async def _on_update():
        new = _create_entities()
        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_on_update)


class DSMRSensor(SensorEntity):
    """Representation of a DSMR sensor keyed by OBIS/property name."""

    def __init__(self, coordinator: DSMRCoordinator, key: str):
        self.coordinator = coordinator
        self._key = key
        self._name = f"DSMR {key}"
        self._unique_id = f"dsmr_homey_{key}"
        self._device_info = DeviceInfo(identifiers={(DOMAIN, "homey_dsmr")}, name="Homey DSMR")
        self._state = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def available(self) -> bool:
        return self.coordinator.available

    @property
    def native_value(self) -> StateType:
        return self.coordinator.get_data().get(self._key)

    @property
    def device_info(self) -> DeviceInfo:
        return self._device_info

    async def async_update(self):
        """Update is handled by coordinator; no blocking network calls here."""
        # State is read directly from coordinator when Home Assistant requests it.
        pass