"""WebSocket coordinator – parses telegrams and pushes data to sensors."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_WS_URL, WATCHDOG_TIMEOUT, MAX_BACKOFF

_LOGGER = logging.getLogger(__name__)

# Full telegram (starts with / … ends with !XXXX\r\n)
TELEGRAM_RE = re.compile(r"/.*?![A-F0-9]{4}\r?\n", re.DOTALL)

# OBIS line: 1-0:1.8.0(00123.456*kWh)
OBIS_RE = re.compile(r"(\d+-\d+:\d+\.\d+\.\d+)\(([\d.]+)\*?([A-Za-z]*)\)")

MAX_BUFFER = 12_000          # ~12 KB
READ_TIMEOUT = 30            # seconds
CYCLE_TIMEOUT = 120          # seconds


class DSMRCoordinator(DataUpdateCoordinator):
    """Fetch, parse and distribute DSMR telegrams."""

    def __init__(self, hass: HomeAssistant, name: str, config: dict):
        super().__init__(hass, _LOGGER, name=name, update_interval=None)
        self.config = config
        self._stop_event = asyncio.Event()
        self._last_telegram = None  # type: float | None
        self._data: Dict[str, Any] = {}
        self._available = False
        self._schema: Dict[str, dict] | None = None

    # ------------------------------------------------------------------ #
    #  Schema loading
    # ------------------------------------------------------------------ #
    async def load_schema(self) -> None:
        schema_path = Path(__file__).parent / "dsmr_schema.json"

        def _load():
            with open(schema_path, encoding="utf-8") as f:
                return json.load(f)

        self._schema = await self.hass.async_add_executor_job(_load)
        _LOGGER.debug("Schema loaded – %d OBIS entries", len(self._schema))


    async def stop(self) -> None:
        self._stop_event.set()
        _LOGGER.debug("Coordinator stop requested")

    # ------------------------------------------------------------------ #
    #  WebSocket listener
    # ------------------------------------------------------------------ #
    async def _connect_and_listen(self) -> None:
        ws_url = self.config.get(CONF_WS_URL)
        if not ws_url:
            raise ConfigEntryNotReady("WebSocket URL missing")
        await self._listen_websocket(ws_url)

    async def _listen_websocket(self, ws_url: str) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        session = aiohttp.ClientSession(timeout=timeout)
        connected = False
        try:
            async with session.ws_connect(
                    ws_url, heartbeat=30, autoping=True, ssl=False
            ) as ws:
                connected = True
                _LOGGER.info("WebSocket connected → %s", ws_url)
                buffer = ""
                self._available = True
                self._last_telegram = time.time()

                while not self._stop_event.is_set():
                    # ---- receive -------------------------------------------------
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=READ_TIMEOUT)
                    except asyncio.TimeoutError:
                        _LOGGER.debug("WS read timeout (%ds)", READ_TIMEOUT)
                    else:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            buffer += msg.data
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            try:
                                buffer += msg.data.decode("latin-1", errors="replace")
                            except Exception as exc:
                                _LOGGER.warning("Binary decode error: %s", exc)
                                continue
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("WebSocket closed")

                    # ---- trim -------------------------------------------------
                    if len(buffer) > MAX_BUFFER:
                        _LOGGER.debug("Buffer trimmed (%d → %d)", len(buffer), MAX_BUFFER // 2)
                        buffer = buffer[-MAX_BUFFER // 2 :]

                    # ---- extract full telegrams -------------------------------
                    processed = False
                    while True:
                        m = TELEGRAM_RE.search(buffer)
                        if not m:
                            break
                        telegram = m.group(0).strip()
                        buffer = buffer[m.end() :]
                        if telegram:
                            await self._parse_telegram(telegram)
                            processed = True

                    if processed:
                        self._last_telegram = time.time()

                    # ---- watchdog --------------------------------------------
                    if time.time() - (self._last_telegram or 0) > WATCHDOG_TIMEOUT:
                        if self._available:
                            _LOGGER.warning(
                                "No telegram for %ds → marking connection unavailable",
                                WATCHDOG_TIMEOUT,
                            )
                        self._available = False
                        self.async_set_updated_data(self._data)
                    else:
                        if not self._available:
                            _LOGGER.info("Telegram received – connection back online")
                        self._available = True

        except asyncio.TimeoutError:
            if not connected:
                raise ConfigEntryNotReady(f"WebSocket connect timeout → {ws_url}")
        except Exception:
            _LOGGER.debug("WebSocket closed – will reconnect")
            raise
        finally:
            await session.close()
            if connected:
                _LOGGER.info("WebSocket session closed")

    # ------------------------------------------------------------------ #
    #  Telegram parsing
    # ------------------------------------------------------------------ #
    async def _parse_telegram(self, telegram: str) -> None:
        self._last_telegram = time.time()
        _LOGGER.debug("=== TELEGRAM START (len=%d) ===", len(telegram))

        values: Dict[str, Any] = {}
        meter_id: str | None = None

        for raw_line in telegram.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m = OBIS_RE.match(line)
            if not m:
                _LOGGER.debug("  → no OBIS match: %s", line)
                continue

            obis, val_str, unit = m.groups()
            _LOGGER.debug("  → OBIS %s = %s %s", obis, val_str, unit)

            schema = self._schema.get(obis, {})
            field = schema.get("description", obis).lower().replace(" ", "_")
            typ = schema.get("type", "string")

            if typ == "float":
                parsed = float(val_str) if val_str else 0.0
            elif typ == "int":
                parsed = int(val_str) if val_str else 0
            elif typ == "datetime":
                clean = val_str.rstrip("W")
                dt = datetime.strptime(clean, "%y%m%d%H%M%S")
                dt = dt.replace(tzinfo=timezone(timedelta(hours=1)))  # CET
                parsed = dt.isoformat()
            else:
                parsed = val_str

            values[field] = parsed
            _LOGGER.debug("    • %s = %s (type=%s)", field, parsed, typ)

            if obis == "0-0:96.1.0":
                meter_id = val_str

        # ----------------------------------------------------------------
        #  Push to coordinator data (only when something changed)
        # ----------------------------------------------------------------
        changed_keys = [k for k, v in values.items() if self._data.get(k) != v]
        if changed_keys:
            self._data.update(values)
            if meter_id:
                self._data["meter_id"] = meter_id
            self._available = True
            _LOGGER.info(
                "Telegram parsed – %d values, %d changed → %s",
                len(values),
                len(changed_keys),
                ", ".join(changed_keys[:5]) + ("…" if len(changed_keys) > 5 else ""),
                )

            # Store for daily cost / peak power
            import_total = values.get("active_energy_import_(total)")
            if import_total is not None:
                today = datetime.now().date()
                last_date = self._data.get("_last_date")
                if last_date != today:
                    self._data["_import_yesterday"] = import_total
                    self._data["_last_date"] = today
                    self._data["_peak_today"] = 0.0

                # Update peak
                current_power = values.get("instantaneous_active_power_import", 0)
                self._data["_peak_today"] = max(self._data.get("_peak_today", 0), current_power)

            self.async_set_updated_data(self._data)
        else:
            _LOGGER.debug("Telegram parsed – no value changes")

        _LOGGER.debug("=== TELEGRAM END ===")

    # ------------------------------------------------------------------ #
    #  Public helpers
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self._available

    def get_data(self) -> dict:
        return self._data.copy()