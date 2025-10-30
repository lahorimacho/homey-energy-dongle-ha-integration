"""DSMR Homey (WebSocket) integration."""

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from .const import DOMAIN
from .coordinator import DSMRCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = DSMRCoordinator(hass, entry.title, entry.data)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Load schema
    try:
        await coordinator.load_schema()
    except Exception as err:
        _LOGGER.error("Failed to load schema: %s", err)
        raise ConfigEntryNotReady from err

    # Start the background task – fire-and-forget
    # NO run_forever() – we call the loop directly
    async def _start_background():
        backoff = 5
        while not coordinator._stop_event.is_set():
            try:
                await coordinator._connect_and_listen()
                backoff = 5
            except ConfigEntryNotReady:
                _LOGGER.debug("WebSocket URL missing – retrying in 30s")
                await asyncio.sleep(30)
            except Exception as err:
                _LOGGER.exception("Unexpected error in DSMR loop: %s", err)
                coordinator._available = False
                coordinator.async_set_updated_data(coordinator._data)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    hass.async_create_task(_start_background(), name="dsmr_homey_task")

    # Forward to platforms
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: DSMRCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.stop()
    return await hass.config_entries.async_unload_platforms(entry, ["sensor"])