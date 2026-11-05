"""The ADARAZ Link server of this Home Assistant instance and per device runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import replace
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from adaraz import (
    DeviceConnected,
    DeviceDisconnected,
    DeviceNotConnected,
    DeviceNotFound,
    DeviceSession,
    Hello,
    LinkEvent,
    LinkServer,
    PairedDevice,
    ProtocolError,
    PunchReceived,
    PunchRecord,
    RelayTimeout,
    State,
    StateChanged,
)
from adaraz.aiohttp_ws import handle_request
from adaraz.const import CLOSE_INTERNAL_ERROR
from homeassistant.components.tag import async_scan_tag
from homeassistant.components.tag.const import DOMAIN as TAG_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.util.hass_dict import HassKey

from .const import (
    CAP_RELAY,
    CARD_ID_PREFIX,
    CONF_CAPS,
    CONF_EXPOSE_UID,
    CONF_PUBLIC_KEY,
    DOMAIN,
    RELAY_TIMEOUT,
    SERVER_SOFTWARE,
    WS_PATH,
)
from .storage import AdarazStorage

if TYPE_CHECKING:
    from . import AdarazConfigEntry

_LOGGER = logging.getLogger(__name__)

DATA_LINK: HassKey[AdarazLink] = HassKey(DOMAIN)
DATA_LINK_LOCK: HassKey[asyncio.Lock] = HassKey(f"{DOMAIN}_lock")

RELAY_ERRORS = ("not_allowed", "invalid", "busy")


async def async_get_link(hass: HomeAssistant) -> AdarazLink:
    """Return the link server of this instance, starting it on first use.

    Called from ``async_setup`` and from the config flow (a flow can run before
    the integration is set up).
    """
    if (link := hass.data.get(DATA_LINK)) is not None:
        return link
    async with hass.data.setdefault(DATA_LINK_LOCK, asyncio.Lock()):
        if (link := hass.data.get(DATA_LINK)) is not None:
            return link
        storage = AdarazStorage(hass)
        await storage.async_load()
        server = LinkServer(
            storage.private_key,
            storage,
            server=SERVER_SOFTWARE,
            name=hass.config.location_name,
        )
        link = AdarazLink(hass, server, storage)
        await link.async_remove_orphans()
        hass.http.register_view(AdarazLinkView(server))
        link.async_start()
        hass.data[DATA_LINK] = link
    return link


class AdarazLinkView(HomeAssistantView):
    """WebSocket endpoint for ADARAZ devices.

    Home Assistant authentication is not used: every connection is a Noise
    handshake that authenticates both sides with their static keys.
    """

    url = WS_PATH
    name = "api:adaraz:ws"
    requires_auth = False

    def __init__(self, server: LinkServer) -> None:
        """Initialize the view."""
        self._server = server

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        """Run one device connection."""
        return await handle_request(self._server, request)


class AdarazLink:
    """One :class:`adaraz.LinkServer` per Home Assistant instance."""

    def __init__(
        self, hass: HomeAssistant, server: LinkServer, storage: AdarazStorage
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.server = server
        self.storage = storage
        #: Runtime of loaded config entries, by device public key. Devices are
        #: identified by their key only, never by the id they report.
        self.devices: dict[bytes, AdarazDevice] = {}
        self._unsubs: list[Callable[[], None]] = []
        #: Key of the card IDs, derived from the server's private key.
        self._card_key = hmac.new(
            storage.private_key, b"adaraz card id", hashlib.sha256
        ).digest()
        storage.before_store = self._before_store

    def card_id(self, uid: str) -> str:
        """Return the ID of a card: ``card_`` and 8 hex digits of a keyed hash.

        It is the same for a card on every reader of this Home Assistant instance
        and does not change. A plain hash of a 4 to 7 byte UID could be reversed by
        trying every UID; without the key (derived from the server's private key,
        which never leaves the private store) it cannot.
        """
        digest = hmac.new(self._card_key, uid.upper().encode(), hashlib.sha256)
        return f"{CARD_ID_PREFIX}{digest.hexdigest()[:8]}"

    @callback
    def async_start(self) -> None:
        """Subscribe to server and Home Assistant events."""
        self._unsubs = [
            self.server.subscribe(self._handle_event),
            self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE, self._handle_core_config
            ),
        ]
        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)

    async def _async_stop(self, _event: Event) -> None:
        """Close all sessions (1001) and pairing windows when Home Assistant stops."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        await self.server.close()

    @callback
    def _handle_core_config(self, _event: Event) -> None:
        self.server.name = self.hass.config.location_name

    @callback
    def async_entry_for_device(self, device: PairedDevice) -> ConfigEntry | None:
        """Return the config entry of a paired device.

        That is the entry with the device's public key, or else the entry with the
        id the device was paired under (it was paired again under a new key, after
        a factory reset, and the flow has not updated the entry yet). The id is the
        one stored at pairing; a device cannot change it later.
        """
        entries = self.hass.config_entries.async_entries(DOMAIN)
        key = device.public_key.hex()
        for entry in entries:
            if entry.data.get(CONF_PUBLIC_KEY) == key:
                return entry
        for entry in entries:
            if entry.unique_id == device.device_id:
                return entry
        return None

    async def async_remove_orphans(self) -> None:
        """Forget paired devices without a config entry (e.g. an interrupted flow)."""
        for device in await self.storage.list_devices():
            if self.async_entry_for_device(device) is None:
                _LOGGER.debug("Removing orphaned device %s", device.device_id)
                await self.storage.remove_device(device.public_key)

    async def async_ensure_device(self, entry: ConfigEntry) -> PairedDevice:
        """Return the stored device of an entry; restore it if it is missing.

        If the device was paired again under a new key (and the flow did not get to
        update the entry), the entry gets that key.
        """
        assert entry.unique_id is not None
        public_key = bytes.fromhex(entry.data[CONF_PUBLIC_KEY])
        if (device := await self.storage.get_device(public_key)) is not None:
            return device
        for other in await self.storage.list_devices():
            if other.device_id == entry.unique_id:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_PUBLIC_KEY: other.public_key.hex()}
                )
                return other
        device = PairedDevice(
            public_key=public_key,
            device_id=entry.unique_id,
            name=entry.data.get("name", ""),
            model=entry.data.get("model", ""),
        )
        await self.storage.add_device(device)
        return device

    async def async_wait_for_session(
        self, public_key: bytes, timeout: float
    ) -> DeviceSession | None:
        """Wait until a device has a session; return it, or None after ``timeout``."""
        if (session := self.server.get_session_by_key(public_key)) is not None:
            return session
        connected = asyncio.Event()

        def listener(event: LinkEvent) -> None:
            if (
                isinstance(event, DeviceConnected)
                and event.device.public_key == public_key
            ):
                connected.set()

        unsubscribe = self.server.subscribe(listener)
        try:
            async with asyncio.timeout(timeout):
                await connected.wait()
        except TimeoutError:
            return None
        finally:
            unsubscribe()
        return self.server.get_session_by_key(public_key)

    async def async_forget(self, public_key: bytes, device_id: str) -> None:
        """Unpair a device and delete its stored state.

        The device is the one stored under ``public_key``, or else the one paired
        under ``device_id`` (paired again under a new key the entry does not know).
        A connected device is told to delete its pairing.
        """
        device = await self.storage.get_device(public_key)
        if device is None:
            for other in await self.storage.list_devices():
                if other.device_id == device_id:
                    device = other
        if device is not None:
            with contextlib.suppress(DeviceNotFound):  # Unpaired meanwhile.
                await self.server.unpair(device.device_id)
            if device.public_key != public_key:
                await self.storage.remove_device(device.public_key)
        else:
            _LOGGER.debug("Device %s was not paired", device_id)
        # Also deletes the delivery state, if any.
        await self.storage.remove_device(public_key)

    async def async_unpair(self, entry: ConfigEntry) -> None:
        """Unpair the device of a removed entry and delete its stored state."""
        assert entry.unique_id is not None
        await self.async_forget(
            bytes.fromhex(entry.data[CONF_PUBLIC_KEY]), entry.unique_id
        )

    def _before_store(self, device: PairedDevice) -> None:
        """Refuse punches that cannot be stored and reported now.

        While Home Assistant stops, writes are deferred, so nothing may be
        acknowledged. Punches of an entry that exists but is not loaded (yet) are
        refused too. The device keeps refused punches and sends them again on its
        next session. Punches of devices without an enabled entry are acknowledged
        and dropped.
        """
        if self.hass.is_stopping:
            raise ProtocolError("Home Assistant is stopping", CLOSE_INTERNAL_ERROR)
        if (
            runtime := self.devices.get(device.public_key)
        ) is not None and runtime.ready:
            return
        entry = self.async_entry_for_device(device)
        if entry is not None and entry.disabled_by is None:
            raise ProtocolError("config entry not loaded", CLOSE_INTERNAL_ERROR)

    @callback
    def _handle_event(self, event: LinkEvent) -> None:
        if (runtime := self.devices.get(event.device.public_key)) is not None:
            runtime.handle_event(event)


class AdarazDevice:
    """Runtime data of a config entry: the device and its current session."""

    def __init__(
        self, hass: HomeAssistant, entry: AdarazConfigEntry, link: AdarazLink
    ) -> None:
        """Initialize from the entry and the current session, if any."""
        assert entry.unique_id is not None
        self.hass = hass
        self.entry = entry
        self.link = link
        self.device_id = entry.unique_id
        self.public_key = bytes.fromhex(entry.data[CONF_PUBLIC_KEY])
        self.caps: tuple[str, ...] = tuple(entry.data.get(CONF_CAPS, ()))
        #: Registry id of the Home Assistant device (set by ``async_setup_entry``).
        self.registry_id = ""
        #: True once the platforms are set up and punches can be delivered.
        self.ready = False
        self.connected = False
        self.hello: Hello | None = None
        self.state: State | None = None
        #: Put the raw card UID in punch attributes (option; a change reloads).
        self.expose_uid = bool(entry.options.get(CONF_EXPOSE_UID, False))
        self._listeners: list[Callable[[], None]] = []
        self._punch_listeners: list[Callable[[PunchRecord], None]] = []
        if (session := link.server.get_session_by_key(self.public_key)) is not None:
            self.connected = True
            self.hello = session.hello
            self.state = session.state

    @property
    def relay_allowed(self) -> bool:
        """The connected device accepts relay commands."""
        return (
            self.connected and self.hello is not None and CAP_RELAY in self.hello.caps
        )

    def card_name(self, card_id: str) -> str | None:
        """Return the name of the card's tag (Settings > Tags), if it has one."""
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(TAG_DOMAIN, TAG_DOMAIN, card_id)
        if entity_id is None or (entry := registry.async_get(entity_id)) is None:
            return None
        return entry.name

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` when the connection or state changes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_add_punch_listener(
        self, listener: Callable[[PunchRecord], None]
    ) -> Callable[[], None]:
        """Call ``listener`` for every new punch."""
        self._punch_listeners.append(listener)
        return lambda: self._punch_listeners.remove(listener)

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @callback
    def async_check_caps(self) -> bool:
        """Store changed capabilities in the entry. Return True if they changed."""
        if self.hello is None or sorted(self.hello.caps) == sorted(self.caps):
            return False
        self.caps = tuple(self.hello.caps)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_CAPS: list(self.caps)}
        )
        return True

    @callback
    def handle_event(self, event: LinkEvent) -> None:
        """Handle an event of the link server for this device."""
        if isinstance(event, PunchReceived):
            self._handle_punch(event.punch)
        elif isinstance(event, StateChanged):
            self.state = State(relay=event.relay, reader_ok=event.reader_ok)
            self._notify()
        elif isinstance(event, DeviceConnected):
            self._handle_connected(event.hello)
        elif isinstance(event, DeviceDisconnected):
            if self.connected:
                _LOGGER.info("%s disconnected", self.device_id)
            self.connected = False
            self.state = None
            self._notify()

    @callback
    def _handle_connected(self, hello: Hello) -> None:
        if not self.connected:
            _LOGGER.info("%s connected", self.device_id)
        self.connected = True
        self.hello = hello
        self.state = None
        if self.registry_id:
            dr.async_get(self.hass).async_update_device(
                self.registry_id, sw_version=hello.fw or None, model=hello.model or None
            )
        if self.async_check_caps():
            # The relay capability decides which entities exist.
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
        self._notify()

    @callback
    def _handle_punch(self, punch: PunchRecord) -> None:
        for listener in list(self._punch_listeners):
            listener(punch)
        if not punch.late and self.registry_id:
            # A live punch is also a tag scan: the card appears in Settings > Tags,
            # where it can be named, and the Tag trigger fires. A late punch is not:
            # an automation must not open a door for a punch from the past.
            self.entry.async_create_task(
                self.hass,
                async_scan_tag(
                    self.hass, self.link.card_id(punch.uid), self.registry_id
                ),
                "adaraz tag scan",
            )

    async def async_relay(self, cmd: str) -> None:
        """Send a relay command; raise HomeAssistantError if it did not execute.

        The command goes to the session of the device with this entry's key.
        """
        try:
            session = self.link.server.get_session_by_key(self.public_key)
            if session is None:
                raise DeviceNotConnected(self.device_id)
            result = await session.relay(cmd, None, RELAY_TIMEOUT)
        except DeviceNotConnected as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            ) from err
        except RelayTimeout as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="relay_timeout"
            ) from err
        if not result.ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=(
                    f"relay_{result.err}"
                    if result.err in RELAY_ERRORS
                    else "relay_failed"
                ),
                translation_placeholders={"error": str(result.err)},
            )
        if result.relay is not None and self.connected:
            current = self.state or State()
            self.state = replace(current, relay=result.relay)
            self._notify()
