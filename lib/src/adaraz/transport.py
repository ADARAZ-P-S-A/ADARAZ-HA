"""Transport abstraction: one binary WebSocket connection.

The protocol code only needs :class:`Transport`. :mod:`adaraz.aiohttp_ws` adapts
aiohttp WebSockets (server and client side); :func:`memory_pipe` gives an in-process
pair for tests.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from .const import CLOSE_NORMAL, CLOSE_PROTOCOL_ERROR, DEFAULT_PATH
from .exceptions import ConnectionClosed, ProtocolError

__all__ = ["MemoryTransport", "Transport", "build_ws_url", "memory_pipe"]


@runtime_checkable
class Transport(Protocol):
    """A message oriented, binary connection (one WebSocket).

    * :meth:`recv` returns the next binary message, or ``None`` once the connection is
      closed (by either side). A text message raises :class:`ProtocolError`.
    * :meth:`send` sends one binary message. It raises :class:`ConnectionClosed` (or
      another exception) if the connection is gone.
    * :meth:`close` closes the connection with a WebSocket close code. It must be
      idempotent and may be called from another task while :meth:`recv` is pending.
    * :attr:`close_code` is the close code of the connection once known.
    """

    async def recv(self) -> bytes | None: ...

    async def send(self, data: bytes) -> None: ...

    async def close(self, code: int = CLOSE_NORMAL) -> None: ...

    @property
    def close_code(self) -> int | None: ...


class MemoryTransport:
    """One end of an in-memory connection created by :func:`memory_pipe`."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | str | None] = asyncio.Queue()
        self._peer: MemoryTransport | None = None
        self._closed = False
        self._close_code: int | None = None
        #: Every message sent by this end, for inspection in tests.
        self.sent: list[bytes | str] = []

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_code(self) -> int | None:
        return self._close_code

    async def recv(self) -> bytes | None:
        if self._closed and self._queue.empty():
            return None
        item = await self._queue.get()
        if item is None:
            return None
        if isinstance(item, str):
            raise ProtocolError("text WebSocket message", CLOSE_PROTOCOL_ERROR)
        return item

    async def send(self, data: bytes) -> None:
        await self._send(bytes(data))

    async def send_text(self, text: str) -> None:
        """Send a text message (to test that peers reject them)."""
        await self._send(text)

    async def _send(self, item: bytes | str) -> None:
        if self._closed or self._peer is None or self._peer._closed:
            raise ConnectionClosed("connection closed")
        self.sent.append(item)
        self._peer._queue.put_nowait(item)

    async def close(self, code: int = CLOSE_NORMAL) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_code = code
        while not self._queue.empty():  # nothing is delivered after a local close
            self._queue.get_nowait()
        self._queue.put_nowait(None)
        peer = self._peer
        if peer is not None and not peer._closed:
            peer._closed = True
            peer._close_code = code
            peer._queue.put_nowait(None)


def memory_pipe() -> tuple[MemoryTransport, MemoryTransport]:
    """Return two connected :class:`MemoryTransport` ends ``(device, server)``."""
    a, b = MemoryTransport(), MemoryTransport()
    a._peer, b._peer = b, a
    return a, b


def build_ws_url(base_url: str) -> str:
    """Map a configured server base URL to the WebSocket URL (protocol section 1).

    ``http`` maps to ``ws`` and ``https`` to ``wss``; the path ``/api/adaraz/ws`` is
    used unless the URL already contains a path.
    """
    parts = urlsplit(base_url.strip())
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme.lower())
    if scheme is None or not parts.netloc:
        raise ValueError(f"unsupported server URL {base_url!r}")
    path = parts.path if parts.path not in ("", "/") else DEFAULT_PATH
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))
