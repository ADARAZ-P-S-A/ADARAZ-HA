"""Device side of ADARAZ Link, in Python.

:class:`LinkDevice` behaves like a RAZ-RCP-1 on the wire: it pairs as the Noise
initiator, runs sessions, delivers its event log from the server's cursor, reports
state and executes relay commands. It is meant for testing servers (and the Home
Assistant integration) without hardware, and as a reference for firmware
implementations. It does not reconnect by itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from ._channel import SecureChannel
from .codes import derive_psk
from .const import (
    CAP_PUNCHES,
    CAP_RELAY,
    CLOSE_NORMAL,
    IDLE_TIMEOUT,
    MAX_NOISE_MESSAGE,
    MAX_PAYLOAD,
    MAX_PUNCH_ITEMS,
    MODE_PAIR,
    MODE_SESSION,
    PAIRED_DEADLINE,
    PING_INTERVAL,
    PROLOGUE_PAIR,
    PROLOGUE_SESSION,
    PULSE_MS_MAX,
    PULSE_MS_MIN,
)
from .exceptions import ConnectionClosed, PairingRejected, ProtocolError
from .messages import (
    SERVER_TRANSPORT_TYPES,
    Ack,
    Cursor,
    Hello,
    LogInfo,
    Message,
    Paired,
    PairHello,
    Punch,
    Punches,
    Relay,
    RelayResult,
    State,
    Unpair,
    Welcome,
    decode,
    encode,
)
from .noise import IK, XXPSK3, DecryptError, HandshakeState, KeyPair, NoiseError
from .server import server_id
from .transport import Transport

__all__ = ["EventLog", "LinkDevice", "PairingInfo"]

_LOGGER = logging.getLogger(__name__)


def _new_epoch() -> str:
    return secrets.token_hex(4)


class EventLog:
    """An append-only event log with an epoch, like the device's punch log."""

    def __init__(self, epoch: str | None = None) -> None:
        self.epoch = epoch or _new_epoch()
        self._records: list[tuple[str, int] | None] = []

    @property
    def count(self) -> int:
        """Number of records (the next index), including deleted ones."""
        return len(self._records)

    def append(self, uid: str, ts: int) -> int:
        self._records.append((uid.upper(), ts))
        return len(self._records) - 1

    def delete(self, index: int) -> None:
        """Delete (blank) a record; this changes the epoch."""
        self._records[index] = None
        self.epoch = _new_epoch()

    def purge(self) -> None:
        """Remove all records; this changes the epoch."""
        self._records.clear()
        self.epoch = _new_epoch()

    def records_from(self, index: int) -> Iterator[Punch]:
        for i in range(max(index, 0), len(self._records)):
            record = self._records[i]
            if record is not None:
                yield Punch(i, record[0], record[1])


@dataclass(frozen=True, slots=True)
class PairingInfo:
    """What the device stores after a successful pairing."""

    server_public_key: bytes
    server_id: str
    server: str
    name: str


class LinkDevice:
    """A simulated ADARAZ device.

    Pairing state (``server_public_key``) lives in memory; copy it between instances
    to simulate persistence.
    """

    def __init__(
        self,
        *,
        device_id: str = "C00F0202",
        private_key: bytes | None = None,
        model: str = "RAZ-RCP-1",
        fw: str = "107",
        name: str = "Test device",
        relay_allowed: bool = False,
        relay_enabled: bool = True,
        log: EventLog | None = None,
        server_public_key: bytes | None = None,
        open_ms: int = 1000,
        ping_interval: float = PING_INTERVAL,
        idle_timeout: float = IDLE_TIMEOUT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keypair = KeyPair.from_private(private_key) if private_key else KeyPair.generate()
        self.device_id = device_id
        self.model = model
        self.fw = fw
        self.name = name
        #: Remote relay control allowed by the administrator (``lnk_rly``).
        self.relay_allowed = relay_allowed
        #: The relay is enabled in the local settings (not ``disabled``).
        self.relay_enabled = relay_enabled
        self.log = log or EventLog()
        self.server_public_key = server_public_key
        self.server_name = ""
        self.server_instance = ""
        self.open_ms = open_ms
        self.relay = False
        self.reader_ok = True
        self._ping_interval = ping_interval
        self._idle_timeout = idle_timeout
        self._clock = clock
        self._channel: SecureChannel | None = None
        self._wake: asyncio.Event | None = None
        self._progress: asyncio.Event | None = None
        self._position: Cursor | None = None
        self._inflight: Punches | None = None
        self._last_pulse = -1e9
        self._pulse_task: asyncio.Task[None] | None = None
        #: Relay commands received, in order (for tests).
        self.relay_commands: list[Relay] = []
        #: Last ``welcome`` received.
        self.welcome: Welcome | None = None

    # -- identity ------------------------------------------------------------------

    @property
    def private_key(self) -> bytes:
        return self._keypair.private

    @property
    def public_key(self) -> bytes:
        return self._keypair.public

    @property
    def is_paired(self) -> bool:
        return self.server_public_key is not None

    @property
    def connected(self) -> bool:
        return self._channel is not None

    @property
    def caps(self) -> tuple[str, ...]:
        return (CAP_PUNCHES, CAP_RELAY) if self.relay_allowed else (CAP_PUNCHES,)

    def hello(self) -> Hello:
        return Hello(
            id=self.device_id,
            model=self.model,
            fw=self.fw,
            name=self.name,
            caps=self.caps,
            log=LogInfo(self.log.epoch, self.log.count),
        )

    # -- local events ----------------------------------------------------------------

    def punch(self, uid: str, ts: int | None = None) -> int:
        """Record a punch in the log (and deliver it if a session is running)."""
        index = self.log.append(uid, int(self._clock()) if ts is None else ts)
        self._kick()
        return index

    def _kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def set_state(self, *, relay: bool | None = None, reader_ok: bool | None = None) -> None:
        changed = False
        if relay is not None and relay != self.relay:
            self.relay, changed = relay, True
        if reader_ok is not None and reader_ok != self.reader_ok:
            self.reader_ok, changed = reader_ok, True
        if changed:
            await self._send_state()

    async def _send_state(self) -> None:
        if self._channel is not None:
            with contextlib.suppress(ConnectionClosed):
                await self._channel.send(State(relay=self.relay, reader_ok=self.reader_ok))

    async def set_relay_allowed(self, allowed: bool) -> None:
        """Change remote relay consent. Revoking it returns the relay to rest.

        The ``relay`` capability in ``hello`` follows on the next session.
        """
        self.relay_allowed = allowed
        if not allowed:
            await self._relay_rest()

    async def _relay_rest(self) -> None:
        self._cancel_pulse()
        if self.relay:
            self.relay = False
            await self._send_state()

    def delete_record(self, index: int) -> None:
        """Delete a log record (new epoch) and restart delivery from index 0."""
        self.log.delete(index)
        self._kick()

    def purge_log(self) -> None:
        """Remove all log records (new epoch)."""
        self.log.purge()
        self._kick()

    async def stop(self) -> None:
        """Close the running session (1000)."""
        if self._channel is not None:
            await self._channel.close(CLOSE_NORMAL)

    def delivered(self) -> bool:
        """True when every log record is acknowledged by the server."""
        return (
            self._inflight is None
            and self._position is not None
            and self._position.epoch == self.log.epoch
            and self._position.next >= self.log.count
        )

    async def wait_delivered(self, timeout: float = 5.0) -> None:
        """Wait until :meth:`delivered` is true."""
        async with asyncio.timeout(timeout):
            while not self.delivered():
                if self._progress is None:
                    await asyncio.sleep(0.01)
                    continue
                self._progress.clear()
                await self._progress.wait()

    @property
    def acked(self) -> Cursor | None:
        """Last acknowledged position in the log."""
        return self._position

    # -- pairing -------------------------------------------------------------------------

    async def pair(self, transport: Transport, code: str) -> PairingInfo:
        """Pair with a server. Raises :class:`PairingRejected` on a wrong code."""
        psk = derive_psk(code)  # raises InvalidCodeError before any traffic
        hs = HandshakeState(XXPSK3, True, prologue=PROLOGUE_PAIR, s=self._keypair, psk=psk)
        try:
            await transport.send(bytes([MODE_PAIR]) + hs.write_message(b""))
            data = await self._recv(transport, 10.0)
            if data is None:
                raise PairingRejected("server closed the connection", transport.close_code)
            try:
                pair_hello = decode(hs.read_message(data))
            except NoiseError as err:
                raise ProtocolError(f"pairing message 2: {err}") from err
            if not isinstance(pair_hello, PairHello):
                raise ProtocolError("expected pair_hello")
            await transport.send(hs.write_message(encode(self.hello())))
            _, recv_cipher = hs.split()
            data = await self._recv(transport, PAIRED_DEADLINE)
            if data is None:
                raise PairingRejected("pairing rejected", transport.close_code)
            try:
                paired = decode(recv_cipher.decrypt_with_ad(b"", data))
            except DecryptError as err:
                raise PairingRejected("server used a different code") from err
            if not isinstance(paired, Paired):
                raise ProtocolError("expected paired")
            remote = hs.remote_static
            assert remote is not None
            if paired.server_id != server_id(remote):
                raise ProtocolError("server_id does not match the server key")
        except ProtocolError as err:
            await transport.close(err.close_code)
            raise
        except PairingRejected:
            await transport.close(CLOSE_NORMAL)
            raise
        self.server_public_key = remote
        self.server_name = pair_hello.server
        self.server_instance = paired.name
        # The server closes the connection (1000); do not wait long for it.
        with contextlib.suppress(TimeoutError, ProtocolError, ConnectionClosed):
            async with asyncio.timeout(2.0):
                while await transport.recv() is not None:
                    pass
        await transport.close(CLOSE_NORMAL)
        return PairingInfo(remote, paired.server_id, pair_hello.server, paired.name)

    @staticmethod
    async def _recv(transport: Transport, timeout: float) -> bytes | None:
        try:
            async with asyncio.timeout(timeout):
                data = await transport.recv()
        except TimeoutError:
            raise ProtocolError("timeout") from None
        if data is not None and len(data) > MAX_NOISE_MESSAGE:
            raise ProtocolError("message too large")
        return data

    # -- session -----------------------------------------------------------------------

    async def run_session(self, transport: Transport) -> int | None:
        """Run one session until it ends; return the WebSocket close code.

        After ``unpair`` the pairing is deleted (``is_paired`` is false).
        """
        if self.server_public_key is None:
            raise RuntimeError("device is not paired")
        hs = HandshakeState(
            IK, True, prologue=PROLOGUE_SESSION, s=self._keypair, rs=self.server_public_key
        )
        try:
            await transport.send(bytes([MODE_SESSION]) + hs.write_message(encode(self.hello())))
            data = await self._recv(transport, 30.0)
            if data is None:
                return transport.close_code
            try:
                welcome = decode(hs.read_message(data))
            except NoiseError as err:
                raise ProtocolError(f"session message 2: {err}") from err
            if not isinstance(welcome, Welcome):
                raise ProtocolError("expected welcome")
        except ProtocolError as err:
            await transport.close(err.close_code)
            return err.close_code
        except ConnectionClosed:
            return transport.close_code

        self.welcome = welcome
        send_cipher, recv_cipher = hs.split()
        channel = SecureChannel(
            transport,
            send_cipher,
            recv_cipher,
            accept=SERVER_TRANSPORT_TYPES,
            ping_interval=self._ping_interval,
            idle_timeout=self._idle_timeout,
        )
        cursor = welcome.cursor
        if cursor is None:
            self._position = Cursor(self.log.epoch, self.log.count)
        elif cursor.epoch == self.log.epoch:
            self._position = Cursor(cursor.epoch, min(cursor.next, self.log.count))
        else:
            self._position = Cursor(self.log.epoch, 0)
        self._inflight = None
        # The first transport message MUST be ``state``: only then does the server
        # consider the session established (protocol section 4).
        try:
            await channel.send(State(relay=self.relay, reader_ok=self.reader_ok))
        except ConnectionClosed:
            return transport.close_code
        self._channel = channel
        self._wake = asyncio.Event()
        self._progress = asyncio.Event()
        tasks = [
            asyncio.create_task(channel.run_liveness()),
            asyncio.create_task(self._sender(channel)),
        ]
        code: int | None = None
        try:
            self._wake.set()
            while True:
                message = await channel.recv()
                if message is None:
                    break
                if await self._handle(channel, message):
                    break
        except ProtocolError as err:
            code = err.close_code
            await channel.close(err.close_code)
        except ConnectionClosed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                    await task
            self._channel = None
            self._wake = None
            self._inflight = None
            if self._progress is not None:
                self._progress.set()
        return code if code is not None else (channel.close_code or transport.close_code)

    async def _handle(self, channel: SecureChannel, message: Message) -> bool:
        """Handle one server message. Return True to end the session."""
        if isinstance(message, Ack):
            inflight = self._inflight
            if inflight is not None and (message.epoch, message.next) == (
                inflight.epoch,
                inflight.next,
            ):
                self._position = Cursor(inflight.epoch, inflight.next)
                self._inflight = None
                if self._wake is not None:
                    self._wake.set()
                if self._progress is not None:
                    self._progress.set()
            return False
        if isinstance(message, Relay):
            self.relay_commands.append(message)
            before = self.relay
            await channel.send(self._execute_relay(message))
            if self.relay != before:
                await self._send_state()
            return False
        if isinstance(message, Unpair):
            _LOGGER.info("server requested unpair")
            self.server_public_key = None
            self._cancel_pulse()
            self.relay = False  # rest state
            await channel.close(CLOSE_NORMAL)
            return True
        return False

    def _execute_relay(self, cmd: Relay) -> RelayResult:
        if not self.relay_allowed or not self.relay_enabled:
            return RelayResult(id=cmd.id, ok=False, err="not_allowed")
        if cmd.cmd == "on":
            self._cancel_pulse()
            self.relay = True
        elif cmd.cmd == "off":
            self._cancel_pulse()
            self.relay = False
        elif cmd.cmd in ("open", "pulse"):
            if cmd.cmd == "pulse" and (
                cmd.ms is None or not PULSE_MS_MIN <= cmd.ms <= PULSE_MS_MAX
            ):
                return RelayResult(id=cmd.id, ok=False, err="invalid")
            now = asyncio.get_running_loop().time()
            if now - self._last_pulse < 1.0:
                return RelayResult(id=cmd.id, ok=False, err="busy")
            self._last_pulse = now
            self._cancel_pulse()
            self.relay = True
            ms = self.open_ms if cmd.cmd == "open" else cmd.ms
            assert ms is not None
            self._pulse_task = asyncio.create_task(self._pulse_off(ms / 1000))
        else:
            return RelayResult(id=cmd.id, ok=False, err="invalid")
        return RelayResult(id=cmd.id, ok=True, relay=self.relay)

    def _cancel_pulse(self) -> None:
        if self._pulse_task is not None:
            self._pulse_task.cancel()
            self._pulse_task = None

    async def _pulse_off(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._pulse_task = None
        self.relay = False
        await self._send_state()

    async def _sender(self, channel: SecureChannel) -> None:
        assert self._wake is not None
        wake = self._wake
        while True:
            await wake.wait()
            wake.clear()
            if self._inflight is not None or self._position is None:
                continue
            if self._position.epoch != self.log.epoch:
                # The log was rewritten while connected: after the ack of the batch
                # in flight (above), start over from index 0 with the new epoch.
                self._position = Cursor(self.log.epoch, 0)
                if self._progress is not None:
                    self._progress.set()
            if self._position.next >= self.log.count:
                continue
            batch = self._build_batch(self._position.next)
            self._inflight = batch
            await channel.send(batch)

    def _build_batch(self, start: int) -> Punches:
        """Up to 100 records from ``start`` that fit into one Noise message (4096 bytes).

        ``items`` is empty when every index from ``start`` on is skipped.
        """
        items: list[Punch] = []
        nxt = self.log.count
        size = len(encode(Punches(epoch=self.log.epoch, from_=start, items=(), next=nxt)))
        for punch in self.log.records_from(start):
            item_size = len(f'[{punch.index},"{punch.uid}",{punch.ts}],')
            if len(items) >= MAX_PUNCH_ITEMS or size + item_size > MAX_PAYLOAD:
                nxt = punch.index
                break
            items.append(punch)
            size += item_size
        return Punches(epoch=self.log.epoch, from_=start, items=tuple(items), next=nxt)
