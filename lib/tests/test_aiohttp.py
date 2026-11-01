"""End to end over real WebSockets (aiohttp server and client)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from adaraz import (
    InMemoryStorage,
    LinkServer,
    PairingRejected,
    PunchReceived,
    build_ws_url,
    generate_code,
    generate_private_key,
)
from adaraz.aiohttp_ws import connect
from adaraz.device import LinkDevice

from .conftest import until


class Env:
    def __init__(self, server: LinkServer, base_url: str, http: aiohttp.ClientSession) -> None:
        self.server = server
        self.base_url = base_url
        self.http = http
        self.events: list[object] = []
        server.subscribe(self.events.append)


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    server = LinkServer(generate_private_key(), InMemoryStorage(), server="Test", name="Dom")
    app = web.Application()
    app.router.add_get("/api/adaraz/ws", server.handle_aiohttp)
    test_server = TestServer(app)
    await test_server.start_server()
    async with aiohttp.ClientSession() as http:
        yield Env(server, str(test_server.make_url("/")).rstrip("/"), http)
    await server.close()
    await test_server.close()


def test_build_ws_url() -> None:
    assert build_ws_url("http://ha.local:8123") == "ws://ha.local:8123/api/adaraz/ws"
    assert build_ws_url("https://x.example/") == "wss://x.example/api/adaraz/ws"
    assert build_ws_url("wss://x.example/custom/path") == "wss://x.example/custom/path"
    assert build_ws_url("ws://10.0.0.2:8765") == "ws://10.0.0.2:8765/api/adaraz/ws"
    with pytest.raises(ValueError):
        build_ws_url("ftp://x")


async def test_pair_session_punch_relay(env: Env) -> None:
    device = LinkDevice(relay_allowed=True, open_ms=50)
    window = env.server.open_pairing()
    transport = await connect(env.http, env.base_url)
    assert transport.ws.protocol == "adaraz.v1"
    await device.pair(transport, window.code)
    assert (await window).public_key == device.public_key

    transport = await connect(env.http, env.base_url)
    task = asyncio.create_task(device.run_session(transport))
    await until(lambda: env.server.is_connected(device.device_id))
    device.punch("04A1B2C3D4E5F6")
    await device.wait_delivered()
    await until(lambda: any(isinstance(e, PunchReceived) for e in env.events))
    result = await env.server.send_relay(device.device_id, "open")
    assert result.ok
    await device.stop()
    assert await asyncio.wait_for(task, 3) == 1000


async def test_wrong_code_over_websocket(env: Env) -> None:
    env.server.open_pairing()
    transport = await connect(env.http, env.base_url)
    with pytest.raises(PairingRejected) as err:
        await LinkDevice().pair(transport, generate_code())
    assert err.value.close_code == 4003


async def test_superseded_over_websocket(env: Env) -> None:
    device = LinkDevice()
    window = env.server.open_pairing()
    await device.pair(await connect(env.http, env.base_url), window.code)
    first = asyncio.create_task(device.run_session(await connect(env.http, env.base_url)))
    await until(lambda: env.server.is_connected(device.device_id))
    twin = LinkDevice(private_key=device.private_key, server_public_key=device.server_public_key)
    second = asyncio.create_task(twin.run_session(await connect(env.http, env.base_url)))
    assert await asyncio.wait_for(first, 5) == 4002
    await until(lambda: twin.connected and env.server.is_connected(device.device_id))
    await twin.stop()
    await asyncio.wait_for(second, 3)


async def _raw_ws(env: Env) -> aiohttp.ClientWebSocketResponse:
    return await env.http.ws_connect(build_ws_url(env.base_url), protocols=("adaraz.v1",))


async def test_text_frame_rejected(env: Env) -> None:
    ws = await _raw_ws(env)
    await ws.send_str("hello")
    msg = await ws.receive()
    assert msg.type == aiohttp.WSMsgType.CLOSE
    assert msg.data == 4000


async def test_large_message_rejected(env: Env) -> None:
    env.server.open_pairing()
    ws = await _raw_ws(env)
    await ws.send_bytes(b"P" + bytes(5000))
    msg = await ws.receive()
    assert msg.type == aiohttp.WSMsgType.CLOSE
    assert msg.data == 4000


async def test_unknown_device_over_websocket(env: Env) -> None:
    device = LinkDevice(server_public_key=env.server.public_key)
    code = await device.run_session(await connect(env.http, env.base_url))
    assert code == 4001
