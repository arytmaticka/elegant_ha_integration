"""WebSocket API client and DataUpdateCoordinator for Elegant LED Controller."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

import aiohttp
import websockets
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    InvalidURI,
    WebSocketException,
)

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    MAX_ZONES,
    PING_INTERVAL,
    RECONNECT_BASE_DELAY,
    RECONNECT_MAX_DELAY,
    WS_PATH,
    WS_PORT,
    DEFAULT_EXTERNAL_CHANGE_DEBOUNCE,
    DEFAULT_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers to parse JavaScript object literals served by the controller
# (lang_account.js). Needed because that file uses unquoted keys and is not
# valid JSON.
# ---------------------------------------------------------------------------

def _extract_balanced_braces(text: str, start: int) -> str | None:
    """Return the text starting at text[start] ('{' char) through the
    matching '}'. Respects string literals (single and double quoted).
    """
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    i = start
    in_str = False
    str_char = ""
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\" and i + 1 < len(text):
                i += 2
                continue
            if c == str_char:
                in_str = False
        else:
            if c == '"' or c == "'":
                in_str = True
                str_char = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _js_object_to_dict(js_text: str) -> dict | None:
    """Convert a JS object literal to a Python dict via json.loads.

    Handles:
      - unquoted identifier/numeric keys:   foo:   / 123:
      - trailing commas before } or ]
    Does NOT handle single-quoted strings inside values (would need a full
    tokenizer). All string values in lang_account.js use double quotes.
    """
    if not js_text:
        return None

    # Quote unquoted keys. The regex looks for { or , followed by an
    # identifier or number directly before a colon. It won't match inside
    # double-quoted strings because string content is never preceded by
    # a bare "{" or "," at the JSON-structure level.
    step1 = re.sub(
        r'([{,])(\s*)([A-Za-z_][A-Za-z_0-9]*|\d+)(\s*):',
        r'\1\2"\3"\4:',
        js_text,
    )
    # Strip trailing commas.
    step2 = re.sub(r",(\s*[}\]])", r"\1", step1)

    try:
        return json.loads(step2)
    except json.JSONDecodeError as err:
        _LOGGER.debug("Failed to parse JS object literal: %s", err)
        return None


def _extract_js_named_object(text: str, js_path: str) -> dict | None:
    """Find an assignment like `<js_path> = { ... };` in `text` and return
    the object parsed as a Python dict.
    """
    # Escape dots in the path for regex and allow whitespace around separators.
    parts = [re.escape(p) for p in js_path.split(".")]
    pattern = r"\s*\.\s*".join(parts) + r"\s*=\s*\{"
    m = re.search(pattern, text)
    if not m:
        return None
    obj_text = _extract_balanced_braces(text, m.end() - 1)
    if not obj_text:
        return None
    return _js_object_to_dict(obj_text)


class ElegantApiClient:
    """WebSocket API client for Elegant LED Controller."""

    def __init__(self, hass: HomeAssistant, host: str) -> None:
        """Initialize the API client."""
        self._hass = hass
        self._host = host
        self._ws = None
        self._message_id: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._push_handlers: list[Callable[[dict], None]] = []
        self._on_connection_lost: Callable[[], None] | None = None
        self._listener_task: asyncio.Task | None = None
        self._connected = False
        self._zone_lock = asyncio.Lock()  # Serialize set_selected_zones + set_zone
        self._last_message_time: float = 0.0
        self._controllers_cache: dict[str, Any] | list[Any] | None = None
        # Parsed content of /lang_account.js:  { "EN": {...}, "PL": {...} }
        # Each inner dict contains: effects / effects_roll / controllers_desc
        self._lang_account_cache: dict[str, dict[str, Any]] | None = None

    @property
    def connected(self) -> bool:
        """Return True if connected."""
        return self._connected and self._ws is not None

    @property
    def host(self) -> str:
        """Return the host address."""
        return self._host

    @property
    def last_message_time(self) -> float:
        """Return the timestamp of the last received message."""
        return self._last_message_time

    @property
    def ws_url(self) -> str:
        """Return the WebSocket URL."""
        return f"ws://{self._host}:{WS_PORT}{WS_PATH}"

    def register_push_handler(self, handler: Callable[[dict], None]) -> None:
        """Register a handler for push events (set_zone, interval)."""
        self._push_handlers.append(handler)

    def set_connection_lost_callback(self, cb: Callable[[], None] | None) -> None:
        """Set the callback invoked when the connection is unexpectedly lost."""
        self._on_connection_lost = cb

    def _next_id(self) -> int:
        """Get next message ID."""
        msg_id = self._message_id
        self._message_id += 1
        return msg_id

    async def connect(self) -> None:
        """Connect to the WebSocket server."""
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=None,  # We handle pings ourselves
                ping_timeout=None,
                close_timeout=5,
            )
            self._connected = True
            # Start from high offset to avoid ID collision with other WS clients
            # (browser sessions broadcast their messages with their own IDs)
            self._message_id = int(time.time()) % 100000 * 100
            self._pending.clear()
            self._listener_task = asyncio.create_task(self._listener())
            _LOGGER.debug("Connected to Elegant controller at %s", self.ws_url)
        except (OSError, WebSocketException, InvalidURI) as err:
            self._connected = False
            raise ConnectionError(
                f"Failed to connect to {self.ws_url}: {err}"
            ) from err

    async def disconnect(self) -> None:
        """Disconnect from the WebSocket server."""
        self._connected = False
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None

        if self._ws:
            try:
                await self._ws.close()
            except WebSocketException:
                pass
            self._ws = None

        # Cancel all pending futures
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def _send(self, msg: dict) -> Any:
        """Send a message and wait for response."""
        if not self._ws or not self._connected:
            raise ConnectionError("Not connected")

        msg_id = msg["id"]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future

        try:
            raw = json.dumps(msg, separators=(",", ":"))
            # _LOGGER.debug("TX: %s", raw)
            await self._ws.send(raw)
            result = await asyncio.wait_for(future, timeout=10.0)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise
        except (ConnectionClosed, ConnectionClosedError) as err:
            self._pending.pop(msg_id, None)
            self._connected = False
            raise ConnectionError(f"Connection lost: {err}") from err

    async def _send_nowait(self, msg: dict) -> None:
        """Send a message without waiting for response (fire-and-forget).

        The controller executes the command immediately upon receiving the
        JSON frame — the response only confirms it. By not awaiting the
        response we eliminate the round-trip delay, matching the behaviour
        of the manufacturer's browser UI.

        The response will still be received by _listener/_dispatch and
        will resolve (and discard) the pending future if one exists.
        """
        if not self._ws or not self._connected:
            raise ConnectionError("Not connected")

        raw = json.dumps(msg, separators=(",", ":"))
        _LOGGER.debug("TX (nowait): %s", raw)
        try:
            await self._ws.send(raw)
        except (ConnectionClosed, ConnectionClosedError) as err:
            self._connected = False
            raise ConnectionError(f"Connection lost: {err}") from err

    async def _listener(self) -> None:
        """Listen for incoming WebSocket messages."""
        cancelled = False
        unexpected_close = False
        try:
            async for raw_msg in self._ws:
                try:
                    msg = json.loads(raw_msg)
                    # _LOGGER.debug("RX: %s", raw_msg)
                    self._last_message_time = time.time()
                    self._dispatch(msg)
                except json.JSONDecodeError:
                    _LOGGER.warning("Received invalid JSON: %s", raw_msg)
            # Iterator exhausted — server closed connection gracefully
            unexpected_close = True
        except (ConnectionClosed, ConnectionClosedError):
            _LOGGER.debug("WebSocket connection closed unexpectedly")
            unexpected_close = True
        except asyncio.CancelledError:
            # Normal cancellation from disconnect()
            cancelled = True
        finally:
            self._connected = False
            # Cancel all pending futures
            for future in self._pending.values():
                if not future.done():
                    if cancelled:
                        future.cancel()
                    else:
                        future.set_exception(ConnectionError("Connection lost"))
            self._pending.clear()
            # Notify reconnect callback only on unexpected close
            if unexpected_close and self._on_connection_lost:
                self._on_connection_lost()

    @callback
    def _dispatch(self, msg: dict) -> None:
        """Dispatch an incoming message to the right handler."""
        if "id" in msg and "response" in msg:
            # This is a response to a request we sent
            msg_id = msg["id"]
            if msg_id in self._pending:
                future = self._pending.pop(msg_id)
                if not future.done():
                    future.set_result(msg["response"])
                return

        # Push event (no id, or event without matching pending)
        if "event" in msg:
            for handler in self._push_handlers:
                try:
                    handler(msg)
                except Exception:
                    _LOGGER.exception("Error in push handler")

    # --- Public API methods ---

    async def get_user_settings(self) -> dict:
        """Get user settings (MAC, IP, etc.)."""
        msg_id = self._next_id()
        return await self._send(
            {"id": msg_id, "event": "get_user_settings", "data": {}}
        )

    async def get_config(self) -> dict:
        """Get full configuration with all zone states."""
        msg_id = self._next_id()
        return await self._send({"id": msg_id, "event": "get_config"})

    async def set_time(self, tz_name: str | None = None) -> bool:
        """Set current time on the controller.

        The controller expects a Unix timestamp in LOCAL time (not UTC).
        If tz_name is provided, we add the current UTC offset of that
        timezone to time.time() (which returns UTC).
        """
        msg_id = self._next_id()
        utc_ts = int(time.time())

        if tz_name:
            try:
                tz = ZoneInfo(tz_name)
                local_dt = datetime.now(tz)
                utc_offset_seconds = int(local_dt.utcoffset().total_seconds())
                unix_ts = utc_ts + utc_offset_seconds
            except Exception:
                _LOGGER.warning(
                    "Failed to resolve timezone '%s', using UTC", tz_name
                )
                unix_ts = utc_ts
        else:
            unix_ts = utc_ts

        return await self._send(
            {"id": msg_id, "event": "set_time", "data": {"unix": unix_ts}}
        )

    async def set_selected_zones(self, idx: int) -> None:
        """Tell the controller which zone(s) the next set_zone applies to.

        The controller firmware requires a set_selected_zones message before
        set_zone — otherwise it applies the command to whichever zones were
        last selected (e.g. by the browser UI or remote), which can affect
        unrelated zones.

        Args:
            idx: Zone index (0-23) to select. Only one zone is selected.
        """
        if not 0 <= idx < MAX_ZONES:
            raise ValueError(f"Zone index {idx} out of range (0-{MAX_ZONES - 1})")
        selection = [False] * MAX_ZONES
        selection[idx] = True
        msg_id = self._next_id()
        await self._send_nowait(
            {"id": msg_id, "event": "set_selected_zones", "data": selection}
        )

    async def set_zone(self, idx: int, **params: Any) -> None:
        """Set zone parameters (fire-and-forget for minimal latency).

        Sends set_selected_zones first to ensure the controller applies
        the command only to the intended zone.  Both messages are sent
        under a lock so that concurrent calls cannot interleave their
        set_selected_zones / set_zone pairs.

        Args:
            idx: Zone index (0-23).
            **params: Parameters to set (is_on, bright, color_1, color_1_hue,
                      white_temperature, color_saturation, color_mode, etc.)
        """
        if not 0 <= idx < MAX_ZONES:
            raise ValueError(f"Zone index {idx} out of range (0-{MAX_ZONES - 1})")
        async with self._zone_lock:
            # Select only this zone before sending the command
            await self.set_selected_zones(idx)

            msg_id = self._next_id()
            data = {"idx": idx, **params}
            await self._send_nowait(
                {"id": msg_id, "event": "set_zone", "data": data}
            )

    async def on_off_all(self, state: bool) -> None:
        """Turn all zones on or off (fire-and-forget for minimal latency)."""
        msg_id = self._next_id()
        await self._send_nowait(
            {"id": msg_id, "event": "on_off_all", "data": state}
        )

    async def ping(self) -> int:
        """Send ping, returns uptime in ms."""
        msg_id = self._next_id()
        return await self._send({"id": msg_id, "event": "ping"})

    async def get_memory(self) -> list[dict]:
        """Get list of memory presets."""
        msg_id = self._next_id()
        return await self._send({"id": msg_id, "event": "get_memory"})

    async def change_memory(self, index: int) -> bool:
        """Activate a memory preset."""
        msg_id = self._next_id()
        return await self._send(
            {"id": msg_id, "event": "change_memory", "data": index}
        )

    async def async_get_controllers(self, *, force_refresh: bool = False) -> dict[str, Any] | list[Any]:
        """Fetch /controllers.json once and cache it."""
        if self._controllers_cache is not None and not force_refresh:
            return self._controllers_cache

        session = async_get_clientsession(self._hass)
        url = f"http://{self._host}/controllers.json"

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()

            try:
                data = await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                # Fallback
                text = await resp.text()
                data = json.loads(text)

        self._controllers_cache = data
      
        return data

    async def async_get_lang_account(
        self, *, force_refresh: bool = False
    ) -> dict[str, dict[str, Any]]:
        """Fetch and parse /lang_account.js. Cached per controller.

        The file contains two JS object literals assigned to
        `libs_language.EN` and `libs_language.PL`. Each holds `effects`,
        `effects_roll` and `controllers_desc` maps indexed by controller
        type id. We extract them and cache for name lookups.
        """
        if self._lang_account_cache is not None and not force_refresh:
            return self._lang_account_cache

        session = async_get_clientsession(self._hass)
        url = f"http://{self._host}/lang_account.js"

        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                # Controller may serve non-UTF-8; try UTF-8 first, fallback
                # to cp1250/iso-8859-2 if that fails (Polish chars).
                try:
                    text = await resp.text(encoding="utf-8")
                except UnicodeDecodeError:
                    raw = await resp.read()
                    for enc in ("cp1250", "iso-8859-2", "latin-1"):
                        try:
                            text = raw.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        text = raw.decode("utf-8", errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Failed to fetch lang_account.js: %s", err)
            self._lang_account_cache = {}
            return self._lang_account_cache

        result: dict[str, dict[str, Any]] = {}
        for lang_code in ("EN", "PL"):
            parsed = _extract_js_named_object(text, f"libs_language.{lang_code}")
            if isinstance(parsed, dict):
                result[lang_code] = parsed

        _LOGGER.debug(
            "lang_account.js parsed: langs=%s, effects types (EN)=%s, (PL)=%s",
            list(result.keys()),
            sorted(
                (k for k in (result.get("EN", {}).get("effects", {}) or {}).keys()),
                key=lambda k: (isinstance(k, str), str(k)),
            ),
            sorted(
                (k for k in (result.get("PL", {}).get("effects", {}) or {}).keys()),
                key=lambda k: (isinstance(k, str), str(k)),
            ),
        )

        self._lang_account_cache = result
        return result

    # ------------------------------------------------------------------
    # Name resolution
    # ------------------------------------------------------------------

    def _pick_effect_name_inline(self, effect: dict[str, Any]) -> str | None:
        """Return a localized effect name from the controllers.json entry,
        or None if the entry has neither name_pl nor name_en.
        """
        lang = (self._hass.config.language or "en").lower()
        preferred = "name_pl" if lang.startswith("pl") else "name_en"
        return (
            effect.get(preferred)
            or effect.get("name_en")
            or effect.get("name_pl")
        )

    def _pick_effect_name_from_lang(
        self, type_id: int, effect_id: int, *, key: str = "effects"
    ) -> str | None:
        """Look up an effect name in the cached lang_account.js data for
        the given controller type and effect id. Tries HA's current
        language first, then the other language.
        """
        if not self._lang_account_cache:
            return None

        lang = (self._hass.config.language or "en").lower()
        primary = "PL" if lang.startswith("pl") else "EN"
        fallback = "EN" if primary == "PL" else "PL"

        for lang_key in (primary, fallback):
            block = self._lang_account_cache.get(lang_key, {})
            effects_root = block.get(key, {}) if isinstance(block, dict) else {}
            if not isinstance(effects_root, dict):
                continue
            # Keys may be int (from parsed JSON numeric keys) or str
            type_block = effects_root.get(type_id) or effects_root.get(str(type_id))
            if not isinstance(type_block, dict):
                continue
            name = type_block.get(effect_id) or type_block.get(str(effect_id))
            if name:
                return str(name)
        return None

    def _pick_effect_name(self, effect: dict[str, Any]) -> str:
        """Legacy one-argument helper — retained for backward compatibility.

        Prefers controllers.json inline name, otherwise falls back to a
        generic ``Effect <id>`` label. Callers that know the controller
        type should use :meth:`_resolve_effect_name` instead, which also
        consults lang_account.js.
        """
        inline = self._pick_effect_name_inline(effect)
        if inline:
            return inline
        return f"Effect {effect.get('id', '?')}"

    def _resolve_effect_name(
        self,
        effect: dict[str, Any],
        *,
        type_id: int,
        key: str = "effects",
    ) -> str:
        """Resolve the best available effect name for a given controller
        type, trying sources in order:
          1. controllers.json inline (name_pl / name_en in the entry)
          2. lang_account.js[lang][key][type_id][effect_id]
          3. ``Effect <id>`` fallback
        """
        inline = self._pick_effect_name_inline(effect)
        if inline:
            return inline
        try:
            eid = int(effect.get("id", -1))
        except (TypeError, ValueError):
            eid = -1
        if eid >= 0:
            from_lang = self._pick_effect_name_from_lang(type_id, eid, key=key)
            if from_lang:
                return from_lang
        return f"Effect {eid}" if eid >= 0 else "Effect ?"

    async def async_get_zone_effects(self, zone) -> dict[int, str]:
        """Return regular (bitfield) effects for a zone: {id: name}."""
        return await self._fetch_zone_effect_map(zone, key="effects")

    async def async_get_zone_roll_effects(self, zone) -> dict[int, str]:
        """Return roll effects (single-choice, real IDs) for a zone: {id: name}.

        Roll effects are applied once when the controller turns on. Unlike
        regular effects, the controller stores a single integer in
        `roll_effect` — there is no bitfield. Not every controller type
        has roll effects.
        """
        return await self._fetch_zone_effect_map(zone, key="effects_roll")

    async def _fetch_zone_effect_map(self, zone, *, key: str) -> dict[int, str]:
        """Fetch and localize an effect list from controllers.json by key.

        Args:
            key: "effects" (regular, bitfield) or "effects_roll" (rollout).
        """
        controllers = await self.async_get_controllers()
        # Make sure lang_account.js is cached so _resolve_effect_name can
        # fall back to it when controllers.json lacks inline names.
        await self.async_get_lang_account()
        
        type_id = zone.get("type", 0)
        effective_type = (
            type_id if (isinstance(type_id, int) and type_id > 0) else 50
        )
        
        type_cfg = controllers.get(str(effective_type), {})
        raw = type_cfg.get(key, []) if isinstance(type_cfg, dict) else []

        # Key by real effect ID — for "effects" == bit position, for
        # "effects_roll" == the integer value written to zone.roll_effect.
        result: dict[int, str] = {}
        with_inline = 0
        with_lang = 0
        with_fallback = 0
        for e in raw:
            if not isinstance(e, dict) or "id" not in e:
                continue
            try:
                eid = int(e["id"])
            except (TypeError, ValueError):
                continue

            inline = self._pick_effect_name_inline(e)
            if inline:
                result[eid] = inline
                with_inline += 1
                continue
            from_lang = self._pick_effect_name_from_lang(
                effective_type, eid, key=key
            )
            if from_lang:
                result[eid] = from_lang
                with_lang += 1
                continue
            result[eid] = f"Effect {eid}"
            with_fallback += 1

        _LOGGER.debug(
            "Zone effects fetched: type=%s (effective=%s) key=%s → "
            "%d entries (inline=%d, lang_account=%d, fallback=%d). "
            "Available controller types in controllers.json: %s",
            type_id, effective_type, key,
            len(result), with_inline, with_lang, with_fallback,
            sorted(
                [k for k in controllers.keys() if isinstance(k, str) and k.isdigit()],
                key=int,
            ) if isinstance(controllers, dict) else "(not a dict)",
        )
        return result


class ElegantCoordinator(DataUpdateCoordinator):
    """Coordinator for Elegant LED Controller.

    Manages WebSocket connection, keepalive pings, and state synchronization.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        time_sync_threshold: int = 5,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        external_change_debounce: float = DEFAULT_EXTERNAL_CHANGE_DEBOUNCE,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
        )
        self.api = ElegantApiClient(hass, host)
        self.api.register_push_handler(self._handle_push_event)
        self.api.set_connection_lost_callback(self._on_connection_lost)
        self._host = host
        self._ping_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._debounce_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._user_settings: dict = {}
        self._zones: list[dict] = []
        self._interval_data: dict = {}
        self._mac: str = ""
        self._shutting_down = False
        self._wifi_rssi: int | None = None
        self.time_sync_threshold: int = time_sync_threshold
        self.poll_interval: int = poll_interval
        self.external_change_debounce: float = external_change_debounce

    @property
    def mac(self) -> str:
        """Return the MAC address of the controller."""
        return self._mac

    @property
    def host(self) -> str:
        """Return the host address of the controller."""
        return self.api.host

    @property
    def user_settings(self) -> dict:
        """Return user settings."""
        return self._user_settings

    @property
    def zones(self) -> list[dict]:
        """Return the list of zone states."""
        return self._zones

    @property
    def interval_data(self) -> dict:
        """Return the latest interval data."""
        return self._interval_data

    @property
    def wifi_rssi(self) -> int | None:
        """Return the WiFi RSSI value."""
        return self._wifi_rssi

    @property
    def last_seen(self) -> float:
        """Return the timestamp of the last received message (HA local time)."""
        return self.api.last_message_time

    async def _async_update_data(self) -> dict:
        """Return the current data.

        This coordinator is push-based (WebSocket) with its own poll loop,
        so _async_update_data simply returns the latest known state.
        It is not called on a schedule (update_interval is None).
        """
        return {"zones": self._zones}

    async def async_setup(self) -> None:
        """Set up the coordinator: connect and fetch initial state."""
        await self.api.connect()
        try:
            await self._initialize_session()
        except Exception:
            await self.api.disconnect()
            raise

        # Start ping keepalive
        self._ping_task = asyncio.create_task(self._ping_loop())
        # Start periodic full state poll (if enabled)
        self._start_poll_loop()

    @callback
    def _on_connection_lost(self) -> None:
        """Handle unexpected connection loss — schedule reconnect."""
        if self._shutting_down:
            return
        _LOGGER.warning("Connection to Elegant controller lost")
        # Cancel ping loop — reconnect will restart it
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        # Cancel poll loop — reconnect will restart it
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None
        # Cancel any pending debounced refresh
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        if not self._reconnect_task or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _initialize_session(self) -> None:
        """Run initial handshake: get_user_settings, get_config, set_time."""
        self._user_settings = await self.api.get_user_settings()
        self._mac = self._user_settings.get("mac", "").replace(":", "").lower()
        _LOGGER.info(
            "Connected to Elegant controller MAC=%s IP=%s",
            self._user_settings.get("mac"),
            self._user_settings.get("ip"),
        )

        config = await self.api.get_config()
        self._zones = config.get("sections_config", [])

        # Pad to MAX_ZONES if needed
        while len(self._zones) < MAX_ZONES:
            self._zones.append(
                {
                    "active": False,
                    "name": f"Elegant Room {len(self._zones) + 1}",
                    "type": 0,
                    "is_on": False,
                    "bright": 100,
                    "color_mode": 0,
                    "color_1": "#FFFFFF",
                    "color_1_hue": 0,
                    "color_2": "#FFFFFF",
                    "color_2_hue": 0,
                    "color_3": "#000000",
                    "color_3_hue": 0,
                    "color_saturation": 0,
                    "white_temperature": 50,
                    "speed_scene": 5,
                    "time_scene": 15,
                    "roll_effect": 1,
                    "scenes": [2, 0, 0, 0],
                    "available_effects": [],
                    "available_roll_effects": [],
                }
            )

        # Get lists of effects for each zone (regular + roll)
        for idx, zone in enumerate(self._zones):
            try:
                effects = await self.api.async_get_zone_effects(zone)
                zone["available_effects"] = effects
            except Exception as err:
                _LOGGER.warning("Failed to fetch effects for zone %d: %s", idx, err)
                zone["available_effects"] = {}
            try:
                roll = await self.api.async_get_zone_roll_effects(zone)
                zone["available_roll_effects"] = roll
            except Exception as err:
                _LOGGER.warning(
                    "Failed to fetch roll effects for zone %d: %s", idx, err
                )
                zone["available_roll_effects"] = {}

        await self.api.set_time(self.hass.config.time_zone)
        
        # Update coordinator data
        self.async_set_updated_data({"zones": self._zones})

    def _infer_color_saturation(self, idx: int) -> None:
        """Infer color_saturation when the controller doesn't send it explicitly.

        When switching from color mode to white mode in the browser UI,
        the controller sends color_1=0xFFFFFF, color_1_hue=0, color_mode=0
        but does NOT send color_saturation=0. This leaves the old saturation
        value (e.g. 100) in the zone dict, causing HA to show the wrong color.

        Fix: if all color indicators point to white, force saturation to 0.
        """
        zone = self._zones[idx]

        # Only apply when color_mode is 0 (static)
        if zone.get("color_mode", 0) != 0:
            return

        color_1 = zone.get("color_1", "").upper().replace("0X", "").replace("#", "")
        hue_1 = zone.get("color_1_hue", 0)

        # All-white with hue=0 means white mode
        if color_1 == "FFFFFF" and hue_1 == 0:
            if zone.get("color_saturation", 0) != 0:
                _LOGGER.debug(
                    "Zone %d: inferred color_saturation=0 (white mode reset)",
                    idx,
                )
                zone["color_saturation"] = 0

    @callback
    def _handle_push_event(self, msg: dict) -> None:
        """Handle push events from the controller.

        Push events come in two formats:
        - From remote/physical: {"event":"set_zone","response":{...full zone state...}}
        - From other browser/client: {"id":N,"event":"set_zone","data":{...partial params...}}
        """
        event = msg.get("event")

        if event == "set_zone":
            # Format 1: push from remote — full zone state in "response"
            if "response" in msg and isinstance(msg["response"], dict):
                zone_data = msg["response"]
                idx = zone_data.get("idx")
                if idx is not None and 0 <= idx < len(self._zones):
                    self._zones[idx].update(zone_data)
                    self._infer_color_saturation(idx)
                    _LOGGER.debug(
                        "Push update (response) zone %d (%s): is_on=%s, bright=%s",
                        idx,
                        self._zones[idx].get("name"),
                        zone_data.get("is_on"),
                        zone_data.get("bright"),
                    )
                    self.async_set_updated_data({"zones": self._zones})
                    # External change may affect other zones — schedule debounced refresh
                    self._schedule_debounced_refresh()

            # Format 2: push from other browser session — partial params in "data"
            elif "data" in msg and isinstance(msg["data"], dict):
                zone_data = msg["data"]
                idx = zone_data.get("idx")
                if idx is not None and 0 <= idx < len(self._zones):
                    for key, value in zone_data.items():
                        if key == "idx":
                            continue
                        # Only accept keys that already exist in zone dict
                        if key not in self._zones[idx]:
                            _LOGGER.debug(
                                "Push (data) zone %d: ignoring unknown key %s",
                                idx, key,
                            )
                            continue
                        # Browser sends hue in 0-255 scale; normalize to 0-360
                        if key in ("color_1_hue", "color_2_hue", "color_3_hue"):
                            if not isinstance(value, (int, float)):
                                continue
                            value = round(value * 360 / 256, 2)
                        self._zones[idx][key] = value
                    self._infer_color_saturation(idx)
                    _LOGGER.debug(
                        "Push update (data) zone %d (%s): %s",
                        idx,
                        self._zones[idx].get("name"),
                        {k: v for k, v in zone_data.items() if k != "idx"},
                    )
                    self.async_set_updated_data({"zones": self._zones})
                    # External change may affect other zones — schedule debounced refresh
                    self._schedule_debounced_refresh()

        elif event == "on_off_all" and "data" in msg:
            # Another client toggled all zones
            state = bool(msg["data"])
            for zone in self._zones:
                zone["is_on"] = state
            _LOGGER.debug("Push on_off_all: %s", state)
            self.async_set_updated_data({"zones": self._zones})
            # on_off_all affects all zones — schedule debounced refresh
            self._schedule_debounced_refresh()

        elif event == "interval" and "data" in msg:
            self._interval_data = msg["data"]
            rssi = msg["data"].get("wifi_rssi")
            if rssi is not None:
                self._wifi_rssi = rssi
            # Check time drift and sync if needed
            self._check_time_sync(msg["data"])
            self.async_set_updated_data({"zones": self._zones})

    def _check_time_sync(self, interval_data: dict) -> None:
        """Check time drift between HA and controller, sync if needed."""
        threshold = self.time_sync_threshold
        if threshold <= 0:
            return  # Disabled

        controller_ts = interval_data.get("timestamp")
        if controller_ts is None:
            return

        # Controller uses local timestamps, so we must compare with local time
        try:

            tz = ZoneInfo(self.hass.config.time_zone)
            local_dt = datetime.now(tz)
            utc_offset = int(local_dt.utcoffset().total_seconds())
            ha_local_ts = int(time.time()) + utc_offset
        except Exception:
            ha_local_ts = int(time.time())

        drift = abs(ha_local_ts - controller_ts)

        if drift > threshold:
            _LOGGER.info(
                "Controller time drift: %ds (HA_local=%d, controller=%d), syncing...",
                drift,
                ha_local_ts,
                controller_ts,
            )
            # Schedule the async set_time call
            self.hass.async_create_task(self._sync_time())

    async def _sync_time(self) -> None:
        """Send set_time to the controller."""
        try:
            await self.api.set_time(self.hass.config.time_zone)
            _LOGGER.debug("Time synced to controller")
        except (ConnectionError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Failed to sync time: %s", err)

    async def _full_state_refresh(self) -> None:
        """Fetch full config from the controller and update all zone states.

        This catches any changes that were missed by individual push events
        (e.g. multi-zone changes where only one zone was broadcast).
        """
        try:
            config = await self.api.get_config()
            new_zones = config.get("sections_config", [])
            for idx, zone_data in enumerate(new_zones):
                if idx < len(self._zones):
                    self._zones[idx].update(zone_data)
            _LOGGER.debug("Full state refresh: updated %d zones", len(new_zones))
            self.async_set_updated_data({"zones": self._zones})
        except (ConnectionError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Full state refresh failed: %s", err)

    def _start_poll_loop(self) -> None:
        """Start or restart the periodic poll loop based on current settings."""
        # Cancel existing poll task
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

        if self.poll_interval > 0:
            self._poll_task = asyncio.create_task(self._poll_loop())
            _LOGGER.debug(
                "Periodic poll loop started (interval=%ds)", self.poll_interval
            )

    async def _poll_loop(self) -> None:
        """Periodically fetch full state from the controller."""
        try:
            while self.api.connected and not self._shutting_down:
                await asyncio.sleep(self.poll_interval)
                if self._shutting_down or not self.api.connected:
                    return
                _LOGGER.debug("Periodic poll: fetching full state")
                await self._full_state_refresh()
        except asyncio.CancelledError:
            return

    @callback
    def _schedule_debounced_refresh(self) -> None:
        """Schedule a debounced full state refresh after an external change.

        If a refresh is already scheduled, the timer is reset (debounced).
        """
        debounce = self.external_change_debounce
        if debounce <= 0:
            return  # Disabled

        # Cancel any pending debounce
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

        loop = self.hass.loop

        def _fire_refresh() -> None:
            self._debounce_handle = None
            self._debounce_task = self.hass.async_create_task(
                self._full_state_refresh()
            )

        self._debounce_handle = loop.call_later(debounce, _fire_refresh)
        _LOGGER.debug(
            "Debounced refresh scheduled in %.1fs", debounce
        )

    async def _ping_loop(self) -> None:
        """Send periodic pings to keep the connection alive."""
        try:
            while self.api.connected:
                await asyncio.sleep(PING_INTERVAL)
                try:
                    await self.api.ping()
                except (ConnectionError, asyncio.TimeoutError):
                    _LOGGER.warning("Ping failed, connection may be lost")
                    if not self._shutting_down:
                        self._reconnect_task = asyncio.create_task(
                            self._reconnect()
                        )
                    return
        except asyncio.CancelledError:
            return

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        if self._shutting_down:
            return

        delay = RECONNECT_BASE_DELAY
        while not self._shutting_down:
            _LOGGER.info("Attempting reconnect in %s seconds...", delay)
            await asyncio.sleep(delay)
            try:
                await self.api.disconnect()
                await self.api.connect()
                await self._initialize_session()
                # Restart ping loop
                if self._ping_task and not self._ping_task.done():
                    self._ping_task.cancel()
                self._ping_task = asyncio.create_task(self._ping_loop())
                # Restart poll loop
                self._start_poll_loop()
                _LOGGER.info("Reconnected successfully")
                return
            except (ConnectionError, asyncio.TimeoutError, OSError) as err:
                _LOGGER.warning("Reconnect failed: %s", err)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def async_shutdown(self) -> None:
        """Shut down the coordinator."""
        self._shutting_down = True
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            try:
                await self._debounce_task
            except asyncio.CancelledError:
                pass
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        await self.api.disconnect()

    async def async_set_zone(self, idx: int, **params: Any) -> None:
        """Set zone parameters and update local state optimistically."""
        await self.api.set_zone(idx, **params)
        # Optimistic local update — command was sent, assume success
        if 0 <= idx < len(self._zones):
            # Normalize hue values: API sends 0-255, internal state uses 0-360
            normalized = dict(params)
            for hue_key in ("color_1_hue", "color_2_hue", "color_3_hue"):
                if hue_key in normalized:
                    normalized[hue_key] = round(
                        normalized[hue_key] * 360 / 256, 2
                    )
            self._zones[idx].update(normalized)
            self._infer_color_saturation(idx)
            self.async_set_updated_data({"zones": self._zones})

    async def async_on_off_all(self, state: bool) -> None:
        """Turn all zones on or off."""
        await self.api.on_off_all(state)
        # Optimistic local update
        for zone in self._zones:
            zone["is_on"] = state
        self.async_set_updated_data({"zones": self._zones})



