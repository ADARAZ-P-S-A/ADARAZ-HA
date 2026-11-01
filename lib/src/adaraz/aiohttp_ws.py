"""aiohttp adapter (server and client side). Requires the ``aiohttp`` extra."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import WSMsgType, web

from .const import CLOSE_NORMAL, CLOSE_PROTOCOL_ERROR, MAX_WS_MESSAGE, SUBPROTOCOL
from .exceptions import ConnectionClosed, ProtocolError
from .transport import build_ws_url

if TYPE_CHECKING:
    from .server import LinkServer

__all__ = ["AiohttpTransport", "connect", "handle_request"]

_CLOSED = (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR)


class AiohttpTransport:
    """:class:`adaraz.Transport` over an aiohttp WebSocket (either side)."""

    def __init__(self, ws: web.WebSocketResponse | aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws

    @property
    def close_code(self) -> int | None:
        return self.ws.close_code

    async def recv(self) -> bytes | None:
        if self.ws.closed:
            return None
        msg = await self.ws.receive()
        if msg.type == WSMsgType.BINARY:
            data: bytes = msg.data
            return data
        if msg.type == WSMsgType.TEXT:
            raise ProtocolError("text WebSocket message", CLOSE_PROTOCOL_ERROR)
        if msg.type in _CLOSED:
            return None
        raise ProtocolError(f"unexpected WebSocket message type {msg.type!r}")

    async def send(self, data: bytes) -> None:
        if self.ws.closed:
            raise ConnectionClosed("WebSocket closed")
        try:
            await self.ws.send_bytes(data)
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as err:
            raise ConnectionClosed(str(err)) from err

    async def close(self, code: int = CLOSE_NORMAL) -> None:
        if self.ws.closed:
            return
        with contextlib.suppress(ConnectionError, RuntimeError, aiohttp.ClientError):
            await self.ws.close(code=code)


async def handle_request(server: LinkServer, request: web.Request) -> web.WebSocketResponse:
    """Accept a WebSocket request and run it as an ADARAZ Link connection."""
    ws = web.WebSocketResponse(protocols=(SUBPROTOCOL,), max_msg_size=MAX_WS_MESSAGE, autoping=True)
    await ws.prepare(request)
    await server.handle_connection(AiohttpTransport(ws))
    return ws


async def connect(
    session: aiohttp.ClientSession, base_url: str, *, timeout: float = 10.0
) -> AiohttpTransport:
    """Open a client WebSocket to a server base URL (device side, for tests/tools)."""
    ws = await session.ws_connect(
        build_ws_url(base_url),
        protocols=(SUBPROTOCOL,),
        max_msg_size=MAX_WS_MESSAGE,
        autoping=True,
        timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=timeout),
    )
    return AiohttpTransport(ws)
