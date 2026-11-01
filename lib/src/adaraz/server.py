"""Server side of ADARAZ Link: pairing windows, sessions, punches and relay commands."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import itertools
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ._channel import SecureChannel
from .codes import derive_psk, generate_code
from .const import (
    CAP_RELAY,
    CLOSE_GOING_AWAY,
    CLOSE_INTERNAL_ERROR,
    CLOSE_NORMAL,
    CLOSE_PAIRING_REJECTED,
    CLOSE_SUPERSEDED,
    CLOSE_UNKNOWN_DEVICE,
    ESTABLISH_TIMEOUT,
    IDLE_TIMEOUT,
    LATE_AFTER,
    MAX_NOISE_MESSAGE,
    MODE_PAIR,
    MODE_SESSION,
    PAIRING_MAX_FAILURES,
    PAIRING_WINDOW,
    PING_INTERVAL,
    PROLOGUE_PAIR,
    PROLOGUE_SESSION,
)
from .exceptions import (
    ConnectionClosed,
    DeviceNotConnected,
    DeviceNotFound,
    PairingCancelled,
    PairingError,
    PairingExpired,
    PairingFailed,
    ProtocolError,
    RelayTimeout,
)
from .messages import (
    DEVICE_TRANSPORT_TYPES,
    Ack,
    Cursor,
    Hello,
    Message,
    Paired,
    PairHello,
    Punches,
    Relay,
    RelayResult,
    State,
    Unpair,
    Welcome,
    decode,
    encode,
)
from .noise import IK, XXPSK3, HandshakeState, KeyPair, NoiseError
from .storage import LinkStorage, PairedDevice, PunchRecord
from .transport import Transport

if TYPE_CHECKING:
    from aiohttp import web

__all__ = [
    "DeviceConnected",
    "DeviceDisconnected",
    "DevicePaired",
    "DeviceSession",
    "DeviceUnpaired",
    "LinkEvent",
    "LinkServer",
    "PairingWindow",
    "PunchReceived",
    "StateChanged",
    "server_id",
]

_LOGGER = logging.getLogger(__name__)

#: Time to wait for the next handshake message.
HANDSHAKE_TIMEOUT = 30.0
#: Default time to wait for ``relay_result``.
RELAY_TIMEOUT = 10.0


def server_id(public_key: bytes) -> str:
    """Server identifier shown on the device: hex SHA-256 of the key, first 16 bytes."""
    return hashlib.sha256(public_key).digest()[:16].hex()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DevicePaired:
    """A device completed pairing."""

    device: PairedDevice


@dataclass(frozen=True, slots=True)
class DeviceUnpaired:
    """A device was removed with :meth:`LinkServer.unpair`."""

    device: PairedDevice


@dataclass(frozen=True, slots=True)
class DeviceConnected:
    """A session was established (the server decrypted the device's first transport
    message). ``hello`` is what the device reported."""

    device: PairedDevice
    hello: Hello


@dataclass(frozen=True, slots=True)
class DeviceDisconnected:
    """A session ended. ``code`` is the WebSocket close code if known."""

    device: PairedDevice
    code: int | None


@dataclass(frozen=True, slots=True)
class StateChanged:
    """The device reported its state."""

    device: PairedDevice
    relay: bool
    reader_ok: bool


@dataclass(frozen=True, slots=True)
class PunchReceived:
    """A new (not duplicate) punch was stored. Check ``punch.late``."""

    device: PairedDevice
    punch: PunchRecord


type LinkEvent = (
    DevicePaired
    | DeviceUnpaired
    | DeviceConnected
    | DeviceDisconnected
    | StateChanged
    | PunchReceived
)
type Listener = Callable[[LinkEvent], Any]


# ---------------------------------------------------------------------------
# Pairing window
# ---------------------------------------------------------------------------


class PairingWindow:
    """An open pairing window. Await it (or :meth:`wait`) for the paired device.

    Raises :class:`PairingExpired`, :class:`PairingFailed` (too many failed attempts)
    or :class:`PairingCancelled`.

    A failed attempt is a handshake that reached the code check: a well-formed pairing
    message 3 whose PSK protected part decrypts with none of the open windows. It
    counts against every open window, because the server cannot tell which code the
    device was given. Connections that end, time out or send a malformed message
    before that point are not counted. Five failed attempts close the window.
    """

    def __init__(self, server: LinkServer, code: str, timeout: float, import_history: bool):
        loop = asyncio.get_running_loop()
        self._server = server
        self.code = code
        self.psk = derive_psk(code)
        self.import_history = import_history
        self.failures = 0
        self.expires_at = time.time() + timeout
        self._future: asyncio.Future[PairedDevice] = loop.create_future()
        self._future.add_done_callback(_consume_exception)
        self._claimed = False
        self._timer = loop.call_later(timeout, self._finish_error, PairingExpired("expired"))

    @property
    def active(self) -> bool:
        """True while a device can still pair with this window."""
        return not self._claimed and not self._future.done()

    def done(self) -> bool:
        return self._future.done()

    def cancel(self) -> None:
        """Close the window. Waiters get :class:`PairingCancelled`."""
        self._finish_error(PairingCancelled("cancelled"))

    async def wait(self) -> PairedDevice:
        return await asyncio.shield(self._future)

    def __await__(self) -> Any:
        return self.wait().__await__()

    # -- internal ----------------------------------------------------------

    def _fail_attempt(self) -> None:
        if not self.active:
            return
        self.failures += 1
        if self.failures >= PAIRING_MAX_FAILURES:
            self._finish_error(PairingFailed("too many failed attempts"))

    def _claim(self) -> None:
        self._claimed = True

    def _finish(self, device: PairedDevice) -> None:
        self._timer.cancel()
        self._server._windows.discard(self)
        if not self._future.done():
            self._future.set_result(device)

    def _finish_error(self, err: PairingError | Exception) -> None:
        self._timer.cancel()
        self._server._windows.discard(self)
        if not self._future.done():
            self._future.set_exception(err)


def _consume_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class DeviceSession:
    """An established session with a device (read-only for applications).

    A session is established once the server decrypted the device's first transport
    message; only established sessions are listed, returned and reported in events.
    """

    def __init__(self, device: PairedDevice, hello: Hello, channel: SecureChannel) -> None:
        self.device = device
        self.hello = hello
        self.channel = channel
        self.connected_at = time.time()
        self.state: State | None = None
        self.cursor: Cursor | None = None
        self._relay_ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[RelayResult]] = {}
        self._finished = asyncio.Event()

    @property
    def device_id(self) -> str:
        return self.device.device_id

    @property
    def relay_allowed(self) -> bool:
        """The device announced the ``relay`` capability in this session."""
        return CAP_RELAY in self.hello.caps

    async def close(self, code: int = CLOSE_NORMAL) -> None:
        await self.channel.close(code)

    async def relay(self, cmd: str, ms: int | None, timeout: float) -> RelayResult:
        request = Relay(id=next(self._relay_ids), cmd=cmd, ms=ms)
        request.validate()
        future: asyncio.Future[RelayResult] = asyncio.get_running_loop().create_future()
        self._pending[request.id] = future
        try:
            try:
                await self.channel.send(request)
            except ConnectionClosed as err:
                raise DeviceNotConnected(self.device_id) from err
            try:
                return await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise RelayTimeout(f"no relay_result from {self.device_id}") from None
        finally:
            self._pending.pop(request.id, None)

    def _resolve(self, result: RelayResult) -> None:
        future = self._pending.get(result.id)
        if future is not None and not future.done():
            future.set_result(result)

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DeviceNotConnected(self.device_id))


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class LinkServer:
    """ADARAZ Link server.

    :param private_key: the server's long-term X25519 private key (32 bytes); keep it
        stable, devices pin the matching public key at pairing.
    :param storage: the application's :class:`LinkStorage`.
    :param server: server software name sent to devices (``pair_hello``/``welcome``).
    :param name: name of this server instance (e.g. the Home Assistant location name).
    :param handshake_timeout: time to wait for each handshake message.
    :param establish_timeout: time a session has, after ``welcome``, to send its first
        transport message; until then it is not established (protocol section 4).
    :param clock: wall clock (Unix time), used for the late flag.
    """

    def __init__(
        self,
        private_key: bytes,
        storage: LinkStorage,
        *,
        server: str = "adaraz",
        name: str = "",
        ping_interval: float = PING_INTERVAL,
        idle_timeout: float = IDLE_TIMEOUT,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        establish_timeout: float = ESTABLISH_TIMEOUT,
        relay_timeout: float = RELAY_TIMEOUT,
        late_after: float = LATE_AFTER,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keypair = KeyPair.from_private(private_key)
        self._storage = storage
        self.server = server
        self.name = name
        self._ping_interval = ping_interval
        self._idle_timeout = idle_timeout
        self._handshake_timeout = handshake_timeout
        self._establish_timeout = establish_timeout
        self._relay_timeout = relay_timeout
        self._late_after = late_after
        self._clock = clock
        self._listeners: list[Listener] = []
        self._windows: set[PairingWindow] = set()
        self._sessions: dict[bytes, DeviceSession] = {}
        self._key_locks: dict[bytes, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- properties ----------------------------------------------------------

    @property
    def public_key(self) -> bytes:
        return self._keypair.public

    @property
    def server_id(self) -> str:
        return server_id(self._keypair.public)

    @property
    def storage(self) -> LinkStorage:
        return self._storage

    @property
    def sessions(self) -> Sequence[DeviceSession]:
        return list(self._sessions.values())

    @property
    def pairing_windows(self) -> Sequence[PairingWindow]:
        return [w for w in self._windows if w.active]

    def get_session(self, device_id: str) -> DeviceSession | None:
        """Return the established session of the device paired under ``device_id``.

        The id is the one stored at pairing; a device cannot change it later.
        """
        for session in self._sessions.values():
            if session.device_id == device_id:
                return session
        return None

    def get_session_by_key(self, public_key: bytes) -> DeviceSession | None:
        """Return the established session of the device with this static public key."""
        return self._sessions.get(public_key)

    def is_connected(self, device_id: str) -> bool:
        return self.get_session(device_id) is not None

    # -- events ----------------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register an event listener; returns a function that unregisters it.

        Listeners are called in the event loop and must not block. A listener may be
        a coroutine function; it is then run as a task.
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: LinkEvent) -> None:
        for listener in list(self._listeners):
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    self._tasks.add(task)
                    task.add_done_callback(self._task_done)
            except Exception:
                _LOGGER.exception("error in ADARAZ Link event listener")

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("error in ADARAZ Link event listener", exc_info=task.exception())

    # -- pairing -----------------------------------------------------------------

    def open_pairing(
        self, timeout: float = PAIRING_WINDOW, *, import_history: bool = True
    ) -> PairingWindow:
        """Open a pairing window and return it; show ``window.code`` to the person.

        The window closes after one successful pairing, after 5 failed attempts (see
        :class:`PairingWindow`) or after ``timeout`` seconds (at most 10 minutes).
        With ``import_history`` the device sends its whole event log in the first
        session; otherwise only records made after pairing.
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        window = PairingWindow(self, generate_code(), min(timeout, PAIRING_WINDOW), import_history)
        self._windows.add(window)
        return window

    # -- devices -------------------------------------------------------------------

    async def _find_device(self, device_id: str) -> PairedDevice:
        for device in await self._storage.list_devices():
            if device.device_id == device_id:
                return device
        raise DeviceNotFound(device_id)

    async def unpair(self, device_id: str) -> None:
        """Forget a device and tell it to delete its pairing (if connected)."""
        device = await self._find_device(device_id)
        await self._storage.remove_device(device.public_key)
        session = self._sessions.get(device.public_key)
        if session is not None:
            with contextlib.suppress(ConnectionClosed):
                await session.channel.send(Unpair())
            await session.close(CLOSE_NORMAL)
        self._emit(DeviceUnpaired(device))

    async def send_relay(
        self, device_id: str, cmd: str, ms: int | None = None, *, timeout: float | None = None
    ) -> RelayResult:
        """Send a relay command and return the device's ``relay_result``.

        ``cmd`` is ``open``, ``on``, ``off`` or ``pulse`` (with ``ms`` 100-10000).
        Raises :class:`ValueError` for a bad command, :class:`DeviceNotConnected` and
        :class:`RelayTimeout`. A refusal by the device is a result with ``ok=False``
        (``err`` ``not_allowed``, ``invalid`` or ``busy``).
        """
        Relay(cmd=cmd, ms=ms).validate()
        session = self.get_session(device_id)
        if session is None:
            raise DeviceNotConnected(device_id)
        return await session.relay(cmd, ms, self._relay_timeout if timeout is None else timeout)

    async def close(self) -> None:
        """Close all sessions and pairing windows."""
        for window in list(self._windows):
            window.cancel()
        for session in list(self._sessions.values()):
            await session.close(CLOSE_GOING_AWAY)

    # -- connections -----------------------------------------------------------------

    async def handle_aiohttp(self, request: web.Request) -> web.WebSocketResponse:
        """aiohttp request handler for the WebSocket endpoint (``/api/adaraz/ws``)."""
        from .aiohttp_ws import handle_request

        return await handle_request(self, request)

    async def handle_connection(self, transport: Transport) -> None:
        """Run one connection until it ends. The transport is closed on return."""
        try:
            first = await self._recv(transport)
            if first is None:
                return
            if len(first) < 1 or len(first) - 1 > MAX_NOISE_MESSAGE:
                raise ProtocolError("bad first message")
            mode, message = first[0], first[1:]
            if mode == MODE_PAIR:
                await self._handle_pairing(transport, message)
            elif mode == MODE_SESSION:
                await self._handle_session(transport, message)
            else:
                raise ProtocolError(f"unknown mode byte 0x{mode:02x}")
        except ProtocolError as err:
            _LOGGER.debug("closing connection (%s): %s", err.close_code, err)
            await transport.close(err.close_code)
        except ConnectionClosed:
            pass
        except Exception:
            _LOGGER.exception("unexpected error in ADARAZ Link connection")
            await transport.close(CLOSE_INTERNAL_ERROR)
        finally:
            await transport.close(CLOSE_NORMAL)

    async def _recv(self, transport: Transport) -> bytes | None:
        try:
            return await asyncio.wait_for(transport.recv(), self._handshake_timeout)
        except TimeoutError:
            raise ProtocolError("handshake timeout") from None

    @staticmethod
    def _decode_handshake(payload: bytes, expected: type[Message]) -> Any:
        message = decode(payload)
        if not isinstance(message, expected):
            raise ProtocolError(f"expected {expected.T!r} in handshake")
        return message

    async def _handle_pairing(self, transport: Transport, message1: bytes) -> None:
        if not self.pairing_windows:
            raise ProtocolError("no pairing window open", CLOSE_PAIRING_REJECTED)
        hs = HandshakeState(XXPSK3, False, prologue=PROLOGUE_PAIR, s=self._keypair)
        try:
            hs.read_message(message1)
            message2 = hs.write_message(encode(PairHello(server=self.server, name=self.name)))
        except NoiseError as err:
            raise ProtocolError(f"pairing message 1: {err}") from err
        await transport.send(message2)

        message3 = await self._recv(transport)
        if message3 is None:
            return
        if len(message3) > MAX_NOISE_MESSAGE:
            raise ProtocolError("message too large")
        # Try every open window: the PSK is only mixed in at the end of message 3.
        window: PairingWindow | None = None
        payload = b""
        reached_psk = False
        candidates = self.pairing_windows
        for candidate in candidates:
            trial = hs.copy()
            trial.psk = candidate.psk
            try:
                payload = trial.read_message(message3)
            except NoiseError:
                # The ``s`` token of message 3 does not depend on the PSK. ``rs`` is
                # only set once it decrypted, so a set ``rs`` means the handshake
                # really reached the PSK check and the code was wrong.
                reached_psk = reached_psk or trial.remote_static is not None
                continue
            window, hs = candidate, trial
            break
        if window is None:
            if not reached_psk:
                # Garbage or a truncated message 3 is not a code guess: not counted.
                raise ProtocolError("pairing message 3 is malformed")
            for candidate in candidates:
                candidate._fail_attempt()
            raise ProtocolError("pairing failed (wrong code)", CLOSE_PAIRING_REJECTED)

        hello: Hello = self._decode_handshake(payload, Hello)
        if not window.active:
            raise ProtocolError("pairing window closed", CLOSE_PAIRING_REJECTED)
        window._claim()
        public_key = hs.remote_static
        assert public_key is not None
        device = PairedDevice(
            public_key=public_key,
            device_id=hello.id,
            name=hello.name,
            model=hello.model,
            fw=hello.fw,
            paired_at=self._clock(),
        )
        try:
            await self._store_paired(device, hello, window.import_history)
        except BaseException as err:
            window._finish_error(err if isinstance(err, Exception) else PairingCancelled())
            raise
        _LOGGER.info("paired device %s (%s)", device.device_id, device.name)
        window._finish(device)
        self._emit(DevicePaired(device))
        send_cipher, _ = hs.split()
        paired = Paired(server_id=self.server_id, name=self.name)
        await transport.send(send_cipher.encrypt_with_ad(b"", encode(paired)))
        await transport.close(CLOSE_NORMAL)

    async def _store_paired(self, device: PairedDevice, hello: Hello, history: bool) -> None:
        storage = self._storage
        # A device id is unique: re-pairing after a factory reset (new key) replaces
        # the old record.
        for other in await storage.list_devices():
            if other.device_id == device.device_id and other.public_key != device.public_key:
                await storage.remove_device(other.public_key)
                old = self._sessions.pop(other.public_key, None)
                if old is not None:
                    await old.close(CLOSE_NORMAL)
        previous = await storage.get_device(device.public_key)
        if previous is not None:
            await storage.add_device(replace(device, paired_at=previous.paired_at))
        else:
            await storage.add_device(device)
        cursor = await storage.load_cursor(device.public_key)
        if cursor is None or cursor.epoch != hello.log.epoch:
            start = 0 if history else hello.log.count
            await storage.save_cursor(device.public_key, Cursor(hello.log.epoch, start))

    async def _handle_session(self, transport: Transport, message1: bytes) -> None:
        hs = HandshakeState(IK, False, prologue=PROLOGUE_SESSION, s=self._keypair)
        try:
            payload = hs.read_message(message1)
        except NoiseError as err:
            # The device does not know our key: it is not paired with this server.
            raise ProtocolError(f"session message 1: {err}", CLOSE_UNKNOWN_DEVICE) from err
        hello: Hello = self._decode_handshake(payload, Hello)
        public_key = hs.remote_static
        assert public_key is not None
        device = await self._storage.get_device(public_key)
        if device is None:
            raise ProtocolError("unknown device", CLOSE_UNKNOWN_DEVICE)
        if hello.id != device.device_id:
            # The identity is the key; the id is fixed at pairing (protocol section 3).
            _LOGGER.warning("device %s sent hello.id %r, closing", device.device_id, hello.id)
            raise ProtocolError("hello.id differs from the paired id", CLOSE_UNKNOWN_DEVICE)

        # Message 1 of IK can be replayed, so nothing may change before the session is
        # established (protocol section 4): read only, answer, wait for proof.
        cursor = await self._storage.load_cursor(public_key)
        welcome = Welcome(server=self.server, name=self.name, cursor=cursor)
        try:
            message2 = hs.write_message(encode(welcome))
        except NoiseError as err:
            raise ProtocolError(f"session message 2: {err}") from err
        await transport.send(message2)
        send_cipher, recv_cipher = hs.split()
        channel = SecureChannel(
            transport,
            send_cipher,
            recv_cipher,
            accept=DEVICE_TRANSPORT_TYPES,
            ping_interval=self._ping_interval,
            idle_timeout=self._idle_timeout,
        )
        try:
            async with asyncio.timeout(self._establish_timeout):
                first = await channel.recv_any()
        except TimeoutError:
            raise ProtocolError("session not established in time") from None
        if first is None:
            return
        # The device holds its static key and read our message 2: established.
        for_app = await channel.filter(first)

        lock = self._key_locks.setdefault(public_key, asyncio.Lock())
        async with lock:
            # Re-read: the device may have been unpaired or re-paired meanwhile.
            current = await self._storage.get_device(public_key)
            if current is None or current.device_id != device.device_id:
                raise ProtocolError("device is no longer paired", CLOSE_UNKNOWN_DEVICE)
            device = current
            old = self._sessions.get(public_key)
            if old is not None:
                await old.close(CLOSE_SUPERSEDED)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(old._finished.wait(), 5)
                if self._sessions.get(public_key) is old:
                    del self._sessions[public_key]

            updated = replace(device, name=hello.name, model=hello.model, fw=hello.fw)
            if updated != device:
                await self._storage.add_device(updated)
                device = updated

            if cursor is None and await self._storage.load_cursor(public_key) is None:
                # No history wanted: the device starts at its current count.
                await self._storage.save_cursor(
                    public_key, Cursor(hello.log.epoch, hello.log.count)
                )
            session = DeviceSession(device, hello, channel)
            session.cursor = cursor or Cursor(hello.log.epoch, hello.log.count)
            self._sessions[public_key] = session

        _LOGGER.debug("session with %s established", device.device_id)
        self._emit(DeviceConnected(device, hello))
        liveness = asyncio.create_task(channel.run_liveness())
        try:
            if for_app:
                await self._dispatch(session, first)
            await self._session_loop(session)
        finally:
            liveness.cancel()
            session._fail_pending()
            if self._sessions.get(public_key) is session:
                del self._sessions[public_key]
            code = channel.close_code or transport.close_code
            self._emit(DeviceDisconnected(session.device, code))
            session._finished.set()

    async def _session_loop(self, session: DeviceSession) -> None:
        channel = session.channel
        while True:
            message = await channel.recv()
            if message is None:
                return
            await self._dispatch(session, message)

    async def _dispatch(self, session: DeviceSession, message: Message) -> None:
        if isinstance(message, Punches):
            await self._on_punches(session, message)
        elif isinstance(message, State):
            session.state = message
            self._emit(StateChanged(session.device, message.relay, message.reader_ok))
        elif isinstance(message, RelayResult):
            session._resolve(message)

    async def _on_punches(self, session: DeviceSession, batch: Punches) -> None:
        now = self._clock()
        device = session.device
        records = [
            PunchRecord(
                device_id=device.device_id,
                epoch=batch.epoch,
                index=p.index,
                uid=p.uid,
                ts=p.ts,
                late=now - p.ts > self._late_after,
                received_at=now,
            )
            for p in batch.items
        ]
        cursor = Cursor(batch.epoch, batch.next)
        new = await self._storage.store_punches(device, records, cursor)
        session.cursor = cursor
        await session.channel.send(Ack(epoch=batch.epoch, next=batch.next))
        for record in new:
            self._emit(PunchReceived(device, record))
