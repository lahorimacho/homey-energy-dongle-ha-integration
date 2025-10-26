"""Config flow so integration can be added through the UI (minimal)."""

from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from .const import (
    CONF_MODE,
    CONF_WS_URL,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_SERIAL_PORT,
    CONF_SERIAL_BAUDRATE,
    DEFAULT_MODE,
    DEFAULT_WS_URL,
    DEFAULT_TCP_HOST,
    DEFAULT_TCP_PORT,
    DEFAULT_SERIAL_PORT,
    DEFAULT_BAUDRATE,
)

class DSMRFlowHandler(config_entries.ConfigFlow, domain="dsmr_homey"):
    """Handle a config flow for DSMR Homey integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    async def async_step_user(self, user_input=None):
        """Handle the initial step from the user."""
        if user_input is not None:
            return self.async_create_entry(title="DSMR Homey", data=user_input)

        schema = vol.Schema({
            vol.Optional(CONF_MODE, default=DEFAULT_MODE): vol.In(["websocket", "tcp", "serial"]),
            vol.Optional(CONF_WS_URL, default=DEFAULT_WS_URL): str,
            vol.Optional(CONF_TCP_HOST, default=DEFAULT_TCP_HOST): str,
            vol.Optional(CONF_TCP_PORT, default=DEFAULT_TCP_PORT): int,
            vol.Optional(CONF_SERIAL_PORT, default=DEFAULT_SERIAL_PORT): str,
            vol.Optional(CONF_SERIAL_BAUDRATE, default=DEFAULT_BAUDRATE): int,
        })
        return self.async_show_form(step_id="user", data_schema=schema)
