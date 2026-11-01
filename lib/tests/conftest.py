from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from adaraz import InMemoryStorage, LinkEvent, LinkServer, generate_private_key, memory_pipe
from adaraz.const import PROLOGUE_SESSION
from adaraz.device import LinkDevice, PairingInfo
from adaraz.messages import State, Welcome, decode, encode
from adaraz.noise import IK, CipherState, HandshakeState, KeyPair
from adaraz.transport import MemoryTransport


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@dataclass
class Harness:
    server: LinkServer
    storage: InMemoryStorage
    events: list[LinkEvent] = field(default_factory=list)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    def of(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]

    def serve(self, transport: MemoryTransport) -> asyncio.Task[None]:
        task = asyncio.create_task(self.server.handle_connection(transport))
        self.tasks.append(task)
        return task

    async def pair(self, device: LinkDevice, code: str) -> PairingInfo:
        dev_end, srv_end = memory_pipe()
        task = self.serve(srv_end)
        try:
            return await device.pair(dev_end, code)
        finally:
            async with asyncio.timeout(2):
                await task

    def start(self, device: LinkDevice) -> Session:
        dev_end, srv_end = memory_pipe()
        server_task = self.serve(srv_end)
        device_task = asyncio.create_task(device.run_session(dev_end))
        self.tasks.append(device_task)
        return Session(self, device, dev_end, server_task, device_task)

    async def paired_device(self, **kwargs: Any) -> LinkDevice:
        history = kwargs.pop("history", True)
        device = LinkDevice(**kwargs)
        window = self.server.open_pairing(import_history=history)
        await self.pair(device, window.code)
        await window
        return device


@dataclass
class Session:
    harness: Harness
    device: LinkDevice
    transport: MemoryTransport
    server_task: asyncio.Task[None]
    device_task: asyncio.Task[int | None]

    async def connected(self) -> Session:
        server = self.harness.server
        await until(lambda: self.device.connected and server.is_connected(self.device.device_id))
        return self

    async def result(self) -> int | None:
        async with asyncio.timeout(3):
            code = await self.device_task
            await self.server_task
        return code

    async def stop(self) -> int | None:
        await self.device.stop()
        return await self.result()


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    storage = InMemoryStorage()
    server = LinkServer(generate_private_key(), storage, server="Test", name="Dom")
    h = Harness(server, storage)
    server.subscribe(h.events.append)
    yield h
    await server.close()
    for task in h.tasks:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


@dataclass
class RawSession:
    """A hand driven device session, for malformed traffic."""

    transport: MemoryTransport
    task: asyncio.Task[None]
    send: CipherState
    recv: CipherState
    welcome: Welcome

    async def send_payload(self, payload: bytes) -> None:
        await self.transport.send(self.send.encrypt_with_ad(b"", payload))

    async def read(self) -> Any:
        data = await self.transport.recv()
        return None if data is None else decode(self.recv.decrypt_with_ad(b"", data))

    async def closed(self) -> int | None:
        async with asyncio.timeout(3):
            await self.task
        return self.transport.close_code


async def raw_session(
    harness: Harness, device: LinkDevice, *, establish: bool = True
) -> RawSession:
    """Run the IK handshake by hand; with ``establish`` also send the first ``state``."""
    dev_end, srv_end = memory_pipe()
    task = harness.serve(srv_end)
    assert device.server_public_key is not None
    hs = HandshakeState(
        IK,
        True,
        prologue=PROLOGUE_SESSION,
        s=KeyPair.from_private(device.private_key),
        rs=device.server_public_key,
    )
    await dev_end.send(b"S" + hs.write_message(encode(device.hello())))
    data = await dev_end.recv()
    assert data is not None
    welcome = decode(hs.read_message(data))
    assert isinstance(welcome, Welcome)
    send, recv = hs.split()
    raw = RawSession(dev_end, task, send, recv, welcome)
    if establish:
        await raw.send_payload(encode(State(relay=False, reader_ok=True)))
    return raw
