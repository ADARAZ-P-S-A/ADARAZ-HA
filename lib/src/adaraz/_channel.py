"""Encrypted message channel shared by the server and the device implementation."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging

from .const import CLOSE_GOING_AWAY, MAX_NOISE_MESSAGE
from .exceptions import ConnectionClosed, ProtocolError
from .messages import Message, Ping, Pong, UnknownMessage, decode, encode
from .noise import CipherState, NoiseError
from .transport import Transport

_LOGGER = logging.getLogger(__name__)


class SecureChannel:
    """Noise transport messages over a :class:`Transport`.

    * serialises sends (the Noise nonce must match the order on the wire),
    * answers ``ping`` with ``pong`` and swallows ``pong`` and unknown types,
    * rejects known types that are not valid in this direction (close 4000),
    * implements liveness (:meth:`run_liveness`).
    """

    def __init__(
        self,
        transport: Transport,
        send_cipher: CipherState,
        recv_cipher: CipherState,
        *,
        accept: frozenset[type[Message]],
        ping_interval: float,
        idle_timeout: float,
    ) -> None:
        self.transport = transport
        self._send_cipher = send_cipher
        self._recv_cipher = recv_cipher
        self._accept = accept
        self._ping_interval = ping_interval
        self._idle_timeout = idle_timeout
        self._lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        self._time = loop.time
        self._last_send = self._last_recv = self._time()
        self._ping_ids = itertools.count(1)
        self.close_code: int | None = None

    async def send(self, message: Message) -> None:
        payload = encode(message)
        async with self._lock:
            if self.close_code is not None:
                raise ConnectionClosed("channel closed")
            data = self._send_cipher.encrypt_with_ad(b"", payload)
            self._last_send = self._time()
            try:
                await self.transport.send(data)
            except ConnectionClosed:
                raise
            except Exception as err:
                raise ConnectionClosed(str(err)) from err

    async def recv(self) -> Message | None:
        """Return the next application message, or ``None`` when the connection ends."""
        while True:
            message = await self.recv_any()
            if message is None:
                return None
            if await self.filter(message):
                return message

    async def recv_any(self) -> Message | None:
        """Receive, decrypt and decode the next transport message, whatever its type.

        Returns ``None`` when the connection ends. Nothing is answered or filtered;
        pass the message to :meth:`filter` before using it.
        """
        data = await self.transport.recv()
        if data is None:
            return None
        self._last_recv = self._time()
        if len(data) > MAX_NOISE_MESSAGE:
            raise ProtocolError("message too large")
        try:
            payload = self._recv_cipher.decrypt_with_ad(b"", data)
        except NoiseError as err:
            raise ProtocolError(f"cannot decrypt transport message: {err}") from err
        return decode(payload)

    async def filter(self, message: Message) -> bool:
        """Handle the channel level part of a message; True if it is for the application.

        ``ping`` is answered with ``pong``; ``pong`` and unknown types are swallowed;
        a known type not valid in this direction raises :class:`ProtocolError`.
        """
        if isinstance(message, Ping):
            # If the connection is gone, the next recv returns None.
            with contextlib.suppress(ConnectionClosed):
                await self.send(Pong(id=message.id))
            return False
        if isinstance(message, (Pong, UnknownMessage)):
            return False
        if type(message) not in self._accept:
            raise ProtocolError(f"unexpected message type {message.T!r}")
        return True

    async def close(self, code: int) -> None:
        if self.close_code is None:
            self.close_code = code
        await self.transport.close(code)

    async def run_liveness(self) -> None:
        """Send ``ping`` when idle and close the connection when the peer is silent."""
        while self.close_code is None:
            now = self._time()
            idle_deadline = self._last_recv + self._idle_timeout
            ping_deadline = self._last_send + self._ping_interval
            if now >= idle_deadline:
                _LOGGER.debug("peer silent for %.0f s, closing", self._idle_timeout)
                await self.close(CLOSE_GOING_AWAY)
                return
            if now >= ping_deadline:
                try:
                    await self.send(Ping(id=next(self._ping_ids)))
                except ConnectionClosed:
                    return
                continue
            await asyncio.sleep(min(idle_deadline, ping_deadline) - now)
