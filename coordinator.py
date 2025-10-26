"""Coordinator: handles connection, parsing and distribution of parsed telegrams."""

from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryNotReady
import aiohttp
import serial_asyncio

from .const import (
    DEFAULT_MODE,
    CONF_MODE,
    CONF_WS_URL,
    CONF_TCP_HOST,
    CONF_TCP_PORT,
    CONF_SERIAL_PORT,
    CONF_SERIAL_BAUDRATE,
    WATCHDOG_TIMEOUT,
    MAX_BACKOFF,
)

_LOGGER = logging.getLogger(__name__)

# Match a full DSMR telegram: starts with "/" and ends with "!XXXX" (CRC)
TELEGRAM_PATTERN = re.compile(r'/.*?![A-F0-9]{4}\r?\n', re.DOTALL)

# Max buffer size before trimming (prevents unbounded growth from partials)
MAX_BUFFER_SIZE = 10240  # ~10 KB, 5x typical telegram

# Read timeout for receives (prevents hangs on idle connections)
READ_TIMEOUT = 30

# Cycle timeout: 120s for initial data delays
CYCLE_TIMEOUT = 120

# Custom DSMR regex (mimicking your attachments)
OBIS_PATTERN = re.compile(r'(\d+-\d+:\d+\.\d+\.\d+)\(([\d.]+)\*?([A-Za-z]*)\)')


class SerialProtocol(asyncio.Protocol):
    """Protocol for serial connection."""
    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        pass  # Handled in main loop


class DSMRCoordinator(DataUpdateCoordinator):
    """Coordinator that reads DSMR telegrams and exposes parsed data."""

    def __init__(self, hass: HomeAssistant, name: str, config: dict):
        super().__init__(hass, _LOGGER, name=name, update_interval=None)
        self.config = config or {}
        self.hass = hass
        self._stop_event = asyncio.Event()
        self._last_telegram_time: float | None = None
        self._data: Dict[str, Any] = {}
        self._available = False
        self._schema = None  # Loaded async

    async def _load_schema(self) -> None:
        """Async schema load (offloads IO to executor)."""
        schema_path = Path(__file__).parent / "dsmr_schema.json"

        def load_schema():
            with open(schema_path) as f:
                return json.load(f)

        self._schema = await self.hass.async_add_executor_job(load_schema)
        _LOGGER.debug(f"DSMR schema loaded from {schema_path} ({len(self._schema)} entries)")

    async def _run_forever(self) -> None:
        """Detached main loop: connects, listens, parses—updates only on full telegrams."""
        backoff = 5
        while not self._stop_event.is_set():
            try:
                # Safety wrapper: timeout entire cycle
                await asyncio.wait_for(self._connect_and_listen(), timeout=CYCLE_TIMEOUT)
                backoff = 5
            except asyncio.TimeoutError:
                _LOGGER.warning("Connection cycle timed out after %ds (no data?); retrying", CYCLE_TIMEOUT)
                self._available = False
                self._data.clear()
                self.async_set_updated_data(self._data)
            except ConfigEntryNotReady:
                _LOGGER.debug("Connection not ready; backing off")
                await asyncio.sleep(30)
            except Exception as err:
                _LOGGER.exception("Error in DSMR connection: %s", err)
                self._available = False
                self._data.clear()
                self.async_set_updated_data(self._data)

                if self._stop_event.is_set():
                    return

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    async def async_stop(self) -> None:
        """Stop the loop."""
        self._stop_event.set()
        _LOGGER.debug("DSMR loop stop requested")

    async def _connect_and_listen(self) -> None:
        """Establish connection in websocket, tcp, or serial mode (looping listener)."""
        if self._schema is None:
            raise ConfigEntryNotReady("Schema not loaded")
        mode = self.config.get(CONF_MODE, DEFAULT_MODE)
        _LOGGER.info("DSMR coordinator connecting in mode=%s", mode)

        if mode == "websocket":
            ws_url = self.config.get(CONF_WS_URL)
            if not ws_url:
                raise ConfigEntryNotReady("WebSocket URL not configured")
            await self._listen_websocket(ws_url)
        elif mode == "tcp":
            await self._listen_tcp(
                self.config.get(CONF_TCP_HOST, "localhost"),
                int(self.config.get(CONF_TCP_PORT, 80))
            )
        elif mode == "serial":
            await self._listen_serial(
                self.config.get(CONF_SERIAL_PORT),
                int(self.config.get(CONF_SERIAL_BAUDRATE, 115200))
            )
        else:
            raise ValueError(f"Unknown mode {mode}")

    async def _listen_websocket(self, ws_url: str) -> None:
        """Listen for telegrams via WebSocket (mimicking your recv/decode; with timeouts)."""
        timeout = aiohttp.ClientTimeout(total=30)
        session = aiohttp.ClientSession(timeout=timeout)
        connected = False
        try:
            async with session.ws_connect(ws_url, heartbeat=30, autoping=True, ssl=False) as ws:
                connected = True
                _LOGGER.info("Connected to WebSocket %s", ws_url)
                buffer = ""
                self._available = True
                self._last_telegram_time = time.time()

                while not self._stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=READ_TIMEOUT)
                    except asyncio.TimeoutError:
                        _LOGGER.debug("WS receive timeout (%ds idle); checking watchdog", READ_TIMEOUT)
                    else:
                        # Mimic your decode: bytes -> latin1 (P1 raw), else str
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            buffer += msg.data
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            try:
                                buffer += msg.data.decode("latin-1", errors="replace")  # Robust like your utf-8/latin1 fallback
                            except Exception as e:
                                _LOGGER.warning("Binary decode failed: %s", e)
                                continue
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            raise ConnectionError("WebSocket closed")
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise ConnectionError(f"WebSocket error: {getattr(msg, 'data', 'unknown')}")

                    # Trim buffer if too large
                    if len(buffer) > MAX_BUFFER_SIZE:
                        _LOGGER.debug("Buffer exceeded %d bytes; trimming", MAX_BUFFER_SIZE)
                        buffer = buffer[-MAX_BUFFER_SIZE // 2 :]

                    # Extract full telegrams (for partial chunks)
                    processed = False
                    while True:
                        match = TELEGRAM_PATTERN.search(buffer)
                        if not match:
                            break
                        telegram = match.group(0).strip()  # Mimic your strip()
                        if not telegram:
                            buffer = buffer[match.end() :]
                            continue
                        buffer = buffer[match.end() :]
                        await self._parse_telegram(telegram)  # Custom parse here
                        processed = True

                    if processed:
                        self._last_telegram_time = time.time()

                    # Watchdog: Mark unavailable if no full telegram
                    if time.time() - (self._last_telegram_time or 0) > WATCHDOG_TIMEOUT:
                        _LOGGER.warning("No complete telegram for %s seconds; unavailable", WATCHDOG_TIMEOUT)
                        self._available = False
                        self.async_set_updated_data(self._data)

        except asyncio.TimeoutError:
            if not connected:
                raise ConfigEntryNotReady(f"WebSocket connect timeout to {ws_url}")
            else:
                _LOGGER.debug("WS connect succeeded but no data in cycle; continuing")
        except Exception as err:
            _LOGGER.debug("WS connection closed: %s", err)
            raise
        finally:
            await session.close()
            if connected:
                _LOGGER.info("WebSocket connection closed")

    async def _parse_telegram(self, telegram: str) -> None:
        """Custom parse mimicking your DSMRParser: line-by-line regex + schema mapping."""
        self._last_telegram_time = time.time()
        try:
            _LOGGER.debug("Processing telegram (len=%d): %s...", len(telegram), telegram[:100])
            values = {}
            meter_id = None
            timestamp = None

            # Line-by-line (mimicking your splitlines)
            for line in telegram.splitlines():
                line = line.strip()
                if not line:
                    continue
                match = OBIS_PATTERN.match(line)
                if match:
                    obis_code, value, unit = match.groups()
                    _LOGGER.debug(f"Matched OBIS: {obis_code} = {value} {unit}")

                    schema_entry = self._schema.get(obis_code, {})
                    field_name = schema_entry.get('description', obis_code).lower().replace(' ', '_')
                    vtype = schema_entry.get('type', 'string')

                    if vtype == 'float':
                        values[field_name] = float(value) if value else 0.0
                    elif vtype == 'int':
                        values[field_name] = int(value) if value else 0
                    elif vtype == 'datetime':
                        ts_str = value.rstrip('W')
                        ts = datetime.strptime(ts_str, '%y%m%d%H%M%S')
                        ts = ts.replace(tzinfo=timezone(timedelta(hours=1)))  # CET +1; adjust if needed
                        values[field_name] = ts.isoformat()
                        timestamp = ts
                    else:
                        values[field_name] = value

                    if obis_code == '0-0:96.1.0':
                        meter_id = value

            # Mimic your return: Update _data with values (timestamp separate if needed)
            changed = any(k not in self._data or self._data[k] != v for k, v in values.items())
            if changed:
                self._data.update(values)
                if meter_id:
                    self._data['meter_id'] = meter_id  # Ensure meter_id
                self._available = True
                self.async_set_updated_data(self._data)
                _LOGGER.debug("Parsed telegram: %d values, %d changed (meter_id: %s)", len(values), sum(1 for k in values if k not in self._data or self._data[k] != values[k]), meter_id)
            else:
                _LOGGER.debug("Telegram parsed but no changes")

        except Exception as exc:
            _LOGGER.warning("Failed to parse telegram (start=%r): %s", telegram[:60], exc)

    async def _listen_tcp(self, host: str, port: int) -> None:
        """Listen for telegrams over TCP (looping; similar to WS)."""
        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_connection(host, port, ssl=False)
        except asyncio.TimeoutError:
            raise ConfigEntryNotReady(f"TCP connect timeout to {host}:{port}")
        try:
            _LOGGER.info("Connected to TCP %s:%s", host, port)
            buffer = ""
            self._available = True
            self._last_telegram_time = time.time()

            while not self._stop_event.is_set():
                try:
                    data = await asyncio.wait_for(reader.read(1024), timeout=READ_TIMEOUT)
                    if not data:
                        raise ConnectionError("TCP connection closed")
                except asyncio.TimeoutError:
                    _LOGGER.debug("TCP read timeout (%ds idle); checking watchdog", READ_TIMEOUT)
                    continue

                buffer += data.decode("latin-1", errors="replace")

                if len(buffer) > MAX_BUFFER_SIZE:
                    _LOGGER.debug("Buffer exceeded %d bytes; trimming", MAX_BUFFER_SIZE)
                    buffer = buffer[-MAX_BUFFER_SIZE // 2 :]

                processed = False
                while True:
                    match = TELEGRAM_PATTERN.search(buffer)
                    if not match:
                        break
                    telegram = match.group(0).strip()
                    if telegram:
                        buffer = buffer[match.end() :]
                        await self._parse_telegram(telegram)
                        processed = True

                if processed:
                    self._last_telegram_time = time.time()

                if time.time() - (self._last_telegram_time or 0) > WATCHDOG_TIMEOUT:
                    _LOGGER.warning("No complete telegram for %s seconds; unavailable", WATCHDOG_TIMEOUT)
                    self._available = False
                    self.async_set_updated_data(self._data)

        finally:
            writer.close()
            await writer.wait_closed()

    async def _listen_serial(self, port: str, baud: int) -> None:
        """Listen for telegrams via serial port (looping)."""
        loop = asyncio.get_running_loop()
        transport = None
        try:
            async with asyncio.timeout(10):
                _, protocol = await serial_asyncio.create_serial_connection(
                    loop, SerialProtocol, port, baudrate=baud
                )
                transport = protocol.transport
        except asyncio.TimeoutError:
            raise ConfigEntryNotReady(f"Serial open timeout on {port}")
        try:
            _LOGGER.info("Connected to serial %s @ %s", port, baud)
            buffer = ""
            self._available = True
            self._last_telegram_time = time.time()

            while not self._stop_event.is_set():
                try:
                    data = await asyncio.wait_for(transport.read(1024), timeout=READ_TIMEOUT)
                    if not data:
                        raise ConnectionError("Serial connection closed")
                except asyncio.TimeoutError:
                    _LOGGER.debug("Serial read timeout (%ds idle); checking watchdog", READ_TIMEOUT)
                    continue

                buffer += data.decode("latin-1", errors="replace")

                if len(buffer) > MAX_BUFFER_SIZE:
                    _LOGGER.debug("Buffer exceeded %d bytes; trimming", MAX_BUFFER_SIZE)
                    buffer = buffer[-MAX_BUFFER_SIZE // 2 :]

                processed = False
                while True:
                    match = TELEGRAM_PATTERN.search(buffer)
                    if not match:
                        break
                    telegram = match.group(0).strip()
                    if telegram:
                        buffer = buffer[match.end() :]
                        await self._parse_telegram(telegram)
                        processed = True

                if processed:
                    self._last_telegram_time = time.time()

                if time.time() - (self._last_telegram_time or 0) > WATCHDOG_TIMEOUT:
                    _LOGGER.warning("No complete telegram for %s seconds; unavailable", WATCHDOG_TIMEOUT)
                    self._available = False
                    self._data.clear()
                    self.async_set_updated_data(self._data)

        finally:
            if transport:
                transport.close()

    @property
    def available(self) -> bool:
        return self._available

    def get_data(self) -> dict:
        return self._data.copy()