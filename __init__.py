"""Integration entrypoint. Sets up the coordinator and platforms (sensor)."""

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.exceptions import ConfigEntryNotReady
from .const import DOMAIN
from .coordinator import DSMRCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DSMR Homey integration from a config entry."""
    config = entry.data

    # Create coordinator
    coordinator = DSMRCoordinator(
        hass,
        name="dsmr_homey",
        config=config,
    )

    # Store coordinator on hass data for platform access
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Load schema asynchronously (offloads IO to avoid blocking)
    try:
        await coordinator._load_schema()
        _LOGGER.debug("DSMR schema loaded successfully")
    except Exception as err:
        _LOGGER.error("Failed to load DSMR schema: %s", err)
        raise ConfigEntryNotReady("DSMR schema load error")

    # Detach background task immediately (fire-and-forget; non-blocking)
    hass.async_create_task(
        coordinator._run_forever(),
        name="dsmr_homey_background_task"
    )
    _LOGGER.debug("DSMR background task detached and running")

    # Forward setup of sensor platform (entities created on first data update)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    return True  # Return instantly—no awaits here


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry and stop coordinator."""
    if entry.entry_id in hass.data[DOMAIN]:
        coordinator: DSMRCoordinator = hass.data[DOMAIN][entry.entry_id]
        # Task will self-stop on unload via _stop_event (set in async_stop if needed)
        hass.async_create_task(coordinator.async_stop())
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, "sensor")
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok