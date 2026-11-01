from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest

from adaraz import (
    Cursor,
    DeviceConnected,
    DeviceDisconnected,
    DeviceNotConnected,
    DeviceNotFound,
    DeviceUnpaired,
    InMemoryStorage,
    LinkServer,
    PunchReceived,
    RelayTimeout,
    StateChanged,
    generate_private_key,
    memory_pipe,
)
from adaraz.device import EventLog, LinkDevice
from adaraz.messages import (
    DEVICE_TRANSPORT_TYPES,
    Ack,
    Message,
    Ping,
    Pong,
    Punch,
    Punches,
    Relay,
    State,
    decode,
    encode,
)
from adaraz.transport import MemoryTransport

from .conftest import Harness, raw_session, until

UID = "04A1B2C3"


# -- session establishment ------------------------------------------------------


async def test_session_known_device(harness: Harness) -> None:
    device = await harness.paired_device(name="Brama")
    session = await harness.start(device).connected()
    await until(lambda: bool(harness.of(StateChanged)))
    connected = harness.of(DeviceConnected)
    assert len(connected) == 1
    assert connected[0].hello.name == "Brama"
    assert connected[0].hello.log.epoch == device.log.epoch
    assert harness.of(StateChanged)[0].reader_ok is True
    server_session = harness.server.get_session(device.device_id)
    assert server_session is not None and not server_session.relay_allowed
    assert device.welcome is not None and device.welcome.name == "Dom"
    assert await session.stop() == 1000
    await until(lambda: bool(harness.of(DeviceDisconnected)))
    assert not harness.server.is_connected(device.device_id)


async def test_unknown_device(harness: Harness) -> None:
    device = LinkDevice(server_public_key=harness.server.public_key)
    assert await harness.start(device).result() == 4001
    assert harness.of(DeviceConnected) == []


async def test_device_paired_with_other_server(harness: Harness) -> None:
    other = LinkServer(generate_private_key(), InMemoryStorage())
    device = await harness.paired_device()
    device.server_public_key = other.public_key
    assert await harness.start(device).result() == 4001


async def test_superseded(harness: Harness) -> None:
    device = await harness.paired_device()
    first = await harness.start(device).connected()
    twin = LinkDevice(private_key=device.private_key, server_public_key=device.server_public_key)
    second = harness.start(twin)
    assert await first.result() == 4002
    await second.connected()
    kinds = [type(e).__name__ for e in harness.events if not isinstance(e, StateChanged)]
    assert kinds == ["DevicePaired", "DeviceConnected", "DeviceDisconnected", "DeviceConnected"]
    assert harness.of(DeviceDisconnected)[0].code == 4002
    assert harness.server.is_connected(device.device_id)
    await second.stop()


async def test_hello_updates_device_record(harness: Harness) -> None:
    device = await harness.paired_device(fw="106")
    device.fw = "107"
    device.name = "Nowa nazwa"
    session = await harness.start(device).connected()
    stored = await harness.storage.get_device(device.public_key)
    assert stored is not None and stored.fw == "107" and stored.name == "Nowa nazwa"
    await session.stop()


# -- punches, cursor, ack -------------------------------------------------------


async def test_history_is_delivered_in_batches(harness: Harness) -> None:
    device = LinkDevice()
    for i in range(250):
        device.punch(UID, 1_700_000_000 + i)
    window = harness.server.open_pairing(import_history=True)
    await harness.pair(device, window.code)
    sizes: list[int] = []
    original = harness.storage.store_punches

    async def counting(dev, punches, cursor):  # type: ignore[no-untyped-def]
        sizes.append(len(punches))
        return await original(dev, punches, cursor)

    harness.storage.store_punches = counting  # type: ignore[method-assign]
    session = await harness.start(device).connected()
    await device.wait_delivered()
    assert sizes == [100, 100, 50]
    assert len(harness.storage.punches) == 250
    assert [p.index for p in harness.storage.punches] == list(range(250))
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 250)
    assert all(p.late for p in harness.storage.punches)
    assert len(harness.of(PunchReceived)) == 250
    await session.stop()


async def test_live_punch_and_late_flag(harness: Harness) -> None:
    device = await harness.paired_device(history=False)
    session = await harness.start(device).connected()
    now = int(time.time())
    device.punch(UID, now - 10)
    device.punch("04FFEE11", now - 301)
    await device.wait_delivered()
    received = harness.of(PunchReceived)
    assert [(e.punch.uid, e.punch.late) for e in received] == [(UID, False), ("04FFEE11", True)]
    assert received[0].punch.device_id == device.device_id
    assert received[0].punch.epoch == device.log.epoch
    await session.stop()


async def test_no_history_skips_old_records(harness: Harness) -> None:
    device = LinkDevice()
    device.punch(UID, 1)
    window = harness.server.open_pairing(import_history=False)
    await harness.pair(device, window.code)
    session = await harness.start(device).connected()
    device.punch(UID, 2)
    await device.wait_delivered()
    assert [p.ts for p in harness.storage.punches] == [2]
    await session.stop()


async def test_null_cursor(harness: Harness) -> None:
    device = await harness.paired_device()
    device.punch(UID, 1)
    del harness.storage.cursors[device.public_key]
    session = await harness.start(device).connected()
    assert device.welcome is not None and device.welcome.cursor is None
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 1)
    device.punch(UID, 2)
    await device.wait_delivered()
    assert [p.ts for p in harness.storage.punches] == [2]
    await session.stop()


async def test_resume_from_cursor(harness: Harness) -> None:
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    for i in range(10):
        device.punch(UID, 1000 + i)
    await device.wait_delivered()
    await session.stop()
    for i in range(10, 15):
        device.punch(UID, 1000 + i)
    session = await harness.start(device).connected()
    assert device.welcome is not None
    assert device.welcome.cursor == Cursor(device.log.epoch, 10)
    await device.wait_delivered()
    assert [p.ts for p in harness.storage.punches] == [1000 + i for i in range(15)]
    await session.stop()


async def test_epoch_change_resends_and_dedups(harness: Harness) -> None:
    device = await harness.paired_device()
    for i in range(10):
        device.punch(UID, 1000 + i)
    session = await harness.start(device).connected()
    await device.wait_delivered()
    await session.stop()
    old_epoch = device.log.epoch
    device.log.delete(3)
    assert device.log.epoch != old_epoch
    device.punch(UID, 2000)
    session = await harness.start(device).connected()
    assert device.welcome is not None and device.welcome.cursor == Cursor(old_epoch, 10)
    await device.wait_delivered()
    assert len(harness.storage.punches) == 11
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 11)
    assert [e.punch.ts for e in harness.of(PunchReceived)][-1] == 2000
    assert len(harness.of(PunchReceived)) == 11
    await session.stop()


async def test_epoch_change_during_session(harness: Harness) -> None:
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    device.punch(UID, 1)
    await device.wait_delivered()
    device.log.purge()
    device.punch(UID, 5)
    await device.wait_delivered()
    assert [p.ts for p in harness.storage.punches] == [1, 5]
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 1)
    await session.stop()


async def test_dedup_same_punch_twice(harness: Harness) -> None:
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    device.punch(UID, 1234)
    device.punch(UID, 1234)
    device.punch(UID, 1235)
    await device.wait_delivered()
    assert [p.ts for p in harness.storage.punches] == [1234, 1235]
    assert len(harness.of(PunchReceived)) == 2
    await session.stop()


async def test_skipped_indices_are_covered(harness: Harness) -> None:
    device = await harness.paired_device()
    for i in range(5):
        device.punch(UID, 100 + i)
    device.log._records[4] = None  # damaged tail record, epoch unchanged
    session = await harness.start(device).connected()
    await device.wait_delivered()
    assert [p.index for p in harness.storage.punches] == [0, 1, 2, 3]
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 5)
    await session.stop()


async def test_ack_after_storage(harness: Harness) -> None:
    """The ack is sent only after store_punches returned."""
    gate = asyncio.Event()
    original = harness.storage.store_punches

    async def slow_store(*args, **kwargs):  # type: ignore[no-untyped-def]
        await gate.wait()
        return await original(*args, **kwargs)

    harness.storage.store_punches = slow_store  # type: ignore[method-assign]
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    device.punch(UID, 1)
    await asyncio.sleep(0.05)
    assert device.acked == Cursor(device.log.epoch, 0)
    gate.set()
    await device.wait_delivered()
    assert device.acked == Cursor(device.log.epoch, 1)
    await session.stop()


async def test_empty_batch_is_acked(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.send_payload(encode(Punches(epoch=device.log.epoch, from_=0, items=(), next=0)))
    ack = await raw.read()
    assert ack == Ack(epoch=device.log.epoch, next=0)
    await raw.transport.close()
    await raw.closed()


async def test_storage_failure_closes_without_ack(harness: Harness) -> None:
    async def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    harness.storage.store_punches = broken  # type: ignore[method-assign]
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    device.punch(UID, 1)
    assert await session.result() == 1011
    assert device.acked == Cursor(device.log.epoch, 0)


# -- relay ----------------------------------------------------------------------


async def test_relay_round_trip(harness: Harness) -> None:
    device = await harness.paired_device(relay_allowed=True, open_ms=50)
    session = await harness.start(device).connected()
    assert harness.server.get_session(device.device_id).relay_allowed  # type: ignore[union-attr]

    result = await harness.server.send_relay(device.device_id, "open")
    assert result.ok and result.relay is True and result.err is None
    await until(lambda: any(e.relay for e in harness.of(StateChanged)))
    await until(lambda: not device.relay)
    await until(lambda: not harness.of(StateChanged)[-1].relay)

    busy = await harness.server.send_relay(device.device_id, "pulse", 200)
    assert not busy.ok and busy.err == "busy"

    on = await harness.server.send_relay(device.device_id, "on")
    assert on.ok and on.relay is True and device.relay
    off = await harness.server.send_relay(device.device_id, "off")
    assert off.ok and off.relay is False and not device.relay
    assert [c.cmd for c in device.relay_commands] == ["open", "pulse", "on", "off"]
    assert device.relay_commands[1].ms == 200
    await session.stop()


async def test_relay_not_allowed(harness: Harness) -> None:
    device = await harness.paired_device(relay_allowed=False)
    session = await harness.start(device).connected()
    result = await harness.server.send_relay(device.device_id, "on")
    assert not result.ok and result.err == "not_allowed"
    assert not device.relay
    await session.stop()


async def test_relay_invalid_from_device(harness: Harness) -> None:
    device = await harness.paired_device(relay_allowed=True)
    session = await harness.start(device).connected()
    channel = harness.server.get_session(device.device_id).channel  # type: ignore[union-attr]
    session_obj = harness.server.get_session(device.device_id)
    assert session_obj is not None
    future = asyncio.get_running_loop().create_future()
    session_obj._pending[99] = future
    await channel.send(Relay(id=99, cmd="explode"))
    result = await asyncio.wait_for(future, 2)
    assert not result.ok and result.err == "invalid"
    await session.stop()


async def test_relay_local_validation(harness: Harness) -> None:
    with pytest.raises(ValueError):
        await harness.server.send_relay("X", "explode")
    with pytest.raises(ValueError):
        await harness.server.send_relay("X", "pulse", 50)
    with pytest.raises(ValueError):
        await harness.server.send_relay("X", "pulse")
    with pytest.raises(DeviceNotConnected):
        await harness.server.send_relay("X", "open")


async def test_relay_timeout(harness: Harness) -> None:
    class Mute(LinkDevice):
        async def _handle(self, channel, message: Message) -> bool:  # type: ignore[no-untyped-def]
            if isinstance(message, Relay):
                return False
            return await super()._handle(channel, message)

    device = Mute(relay_allowed=True)
    window = harness.server.open_pairing()
    await harness.pair(device, window.code)
    session = await harness.start(device).connected()
    with pytest.raises(RelayTimeout):
        await harness.server.send_relay(device.device_id, "on", timeout=0.1)
    await session.stop()


async def test_relay_fails_on_disconnect(harness: Harness) -> None:
    class Mute(LinkDevice):
        async def _handle(self, channel, message: Message) -> bool:  # type: ignore[no-untyped-def]
            if isinstance(message, Relay):
                await channel.close(1000)
                return True
            return await super()._handle(channel, message)

    device = Mute(relay_allowed=True)
    window = harness.server.open_pairing()
    await harness.pair(device, window.code)
    await harness.start(device).connected()
    with pytest.raises(DeviceNotConnected):
        await harness.server.send_relay(device.device_id, "on", timeout=2)


# -- unpair ---------------------------------------------------------------------


async def test_unpair_connected(harness: Harness) -> None:
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    key = device.server_public_key
    await harness.server.unpair(device.device_id)
    assert await session.result() == 1000
    assert not device.is_paired
    assert await harness.storage.list_devices() == []
    assert len(harness.of(DeviceUnpaired)) == 1
    device.server_public_key = key
    assert await harness.start(device).result() == 4001


async def test_unpair_offline_and_unknown(harness: Harness) -> None:
    device = await harness.paired_device()
    await harness.server.unpair(device.device_id)
    assert await harness.storage.list_devices() == []
    with pytest.raises(DeviceNotFound):
        await harness.server.unpair(device.device_id)


# -- liveness -------------------------------------------------------------------


async def test_ping_keeps_session_alive() -> None:
    storage = InMemoryStorage()
    server = LinkServer(generate_private_key(), storage, ping_interval=0.05, idle_timeout=0.2)
    from .conftest import Harness as H

    harness = H(server, storage)
    device = await harness.paired_device(ping_interval=0.05, idle_timeout=0.2)
    session = await harness.start(device).connected()
    await asyncio.sleep(0.5)
    assert server.is_connected(device.device_id) and device.connected
    await session.stop()


async def test_silent_device_is_dropped() -> None:
    storage = InMemoryStorage()
    server = LinkServer(generate_private_key(), storage, ping_interval=0.05, idle_timeout=0.2)
    from .conftest import Harness as H

    harness = H(server, storage)
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    ping = await raw.read()
    assert isinstance(ping, Ping)
    assert await raw.closed() == 1001
    assert not server.is_connected(device.device_id)


async def test_server_answers_ping(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.send_payload(encode(Ping(id=42)))
    assert await raw.read() == Pong(id=42)
    await raw.transport.close()
    await raw.closed()


# -- malformed traffic ----------------------------------------------------------


async def test_message_too_large(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.transport.send(bytes(4097))
    assert await raw.closed() == 4000


async def test_text_message_rejected(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.transport.send_text('{"t":"ping"}')
    assert await raw.closed() == 4000


async def test_bad_json_rejected(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.send_payload(b"{not json")
    assert await raw.closed() == 4000


async def test_unexpected_type_rejected(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.send_payload(encode(Ack(epoch="00000000", next=0)))
    assert await raw.closed() == 4000


async def test_unknown_type_ignored(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.send_payload(b'{"t":"future_thing","x":1}')
    await raw.send_payload(encode(Ping(id=1)))
    assert await raw.read() == Pong(id=1)
    await raw.transport.close()
    await raw.closed()


async def test_bad_ciphertext_rejected(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device)
    await raw.transport.send(bytes(40))
    assert await raw.closed() == 4000


async def test_wrong_handshake_payload(harness: Harness) -> None:
    from adaraz.const import PROLOGUE_SESSION
    from adaraz.noise import IK, HandshakeState, KeyPair

    device = await harness.paired_device()
    dev_end, srv_end = memory_pipe()
    task = harness.serve(srv_end)
    hs = HandshakeState(
        IK,
        True,
        prologue=PROLOGUE_SESSION,
        s=KeyPair.from_private(device.private_key),
        rs=device.server_public_key,
    )
    await dev_end.send(b"S" + hs.write_message(encode(Ping(id=1))))
    await task
    assert dev_end.close_code == 4000


# -- establishment (protocol section 4) -------------------------------------------


def _server(**kwargs: Any) -> tuple[Harness, LinkServer]:
    storage = InMemoryStorage()
    server = LinkServer(generate_private_key(), storage, **kwargs)
    harness = Harness(server, storage)
    server.subscribe(harness.events.append)
    return harness, server


async def _replay(harness: Harness, message1: bytes | str) -> MemoryTransport:
    assert isinstance(message1, bytes)
    dev_end, srv_end = memory_pipe()
    harness.serve(srv_end)
    await dev_end.send(message1)
    assert await dev_end.recv() is not None  # welcome, unreadable without the key
    return dev_end


async def test_replayed_message1_does_not_kick_live_session() -> None:
    harness, server = _server(establish_timeout=0.2)
    device = await harness.paired_device(name="Stara", relay_allowed=True)
    first = await harness.start(device).connected()
    recorded = first.transport.sent[0]  # mode byte + IK message 1 (old hello)
    await first.stop()
    device.name = "Nowa"
    live = await harness.start(device).connected()
    session = server.get_session(device.device_id)
    events_before = list(harness.events)

    # Replay without any transport message: closed after the timeout, nothing changed.
    attacker = await _replay(harness, recorded)
    assert await attacker.recv() is None
    assert attacker.close_code == 4000
    # Replay followed by a forged transport message: closed at once.
    attacker = await _replay(harness, recorded)
    await attacker.send(bytes(40))
    assert await attacker.recv() is None
    assert attacker.close_code == 4000

    assert server.get_session(device.device_id) is session
    assert device.connected and not live.device_task.done()
    assert harness.events == events_before
    stored = await harness.storage.get_device(device.public_key)
    assert stored is not None and stored.name == "Nowa"
    result = await server.send_relay(device.device_id, "on")
    assert result.ok and device.relay
    await live.stop()
    await server.close()


async def test_session_is_established_by_first_transport_message(harness: Harness) -> None:
    device = await harness.paired_device(fw="106")
    del harness.storage.cursors[device.public_key]
    device.fw = "107"
    raw = await raw_session(harness, device, establish=False)
    await asyncio.sleep(0.05)
    # Not established: no session, no event, nothing written.
    assert not harness.server.is_connected(device.device_id)
    assert harness.of(DeviceConnected) == []
    assert device.public_key not in harness.storage.cursors
    stored = await harness.storage.get_device(device.public_key)
    assert stored is not None and stored.fw == "106"

    await raw.send_payload(encode(State(relay=True, reader_ok=True)))
    await until(lambda: bool(harness.of(StateChanged)))
    assert harness.server.is_connected(device.device_id)
    assert len(harness.of(DeviceConnected)) == 1
    assert harness.of(StateChanged)[0].relay is True
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 0)
    stored = await harness.storage.get_device(device.public_key)
    assert stored is not None and stored.fw == "107"
    await raw.transport.close()
    await raw.closed()


async def test_first_message_ping_establishes(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device, establish=False)
    await raw.send_payload(encode(Ping(id=3)))
    assert await raw.read() == Pong(id=3)
    assert harness.server.is_connected(device.device_id)
    await raw.transport.close()
    await raw.closed()


async def test_first_message_of_wrong_direction_rejected(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device, establish=False)
    await raw.send_payload(encode(Ack(epoch="00000000", next=0)))
    assert await raw.closed() == 4000
    assert harness.of(DeviceConnected) == []


async def test_unestablished_session_is_closed_after_timeout() -> None:
    harness, _ = _server(establish_timeout=0.1)
    device = await harness.paired_device()
    raw = await raw_session(harness, device, establish=False)
    assert await raw.closed() == 4000
    assert harness.of(DeviceConnected) == []
    assert harness.of(DeviceDisconnected) == []


async def test_default_establish_timeout_is_ten_seconds() -> None:
    from adaraz.const import ESTABLISH_TIMEOUT

    assert ESTABLISH_TIMEOUT == 10.0
    _, server = _server()
    assert server._establish_timeout == 10.0


async def test_unpaired_before_establishment(harness: Harness) -> None:
    device = await harness.paired_device()
    raw = await raw_session(harness, device, establish=False)
    await harness.server.unpair(device.device_id)
    await raw.send_payload(encode(State()))
    assert await raw.closed() == 4001
    assert harness.of(DeviceConnected) == []


async def test_device_sends_state_first(harness: Harness, monkeypatch: Any) -> None:
    from adaraz._channel import SecureChannel

    first: list[Message | None] = []
    original = SecureChannel.recv_any

    async def recording(self: SecureChannel) -> Message | None:
        message = await original(self)
        if self._accept is DEVICE_TRANSPORT_TYPES and not first:
            first.append(message)
        return message

    monkeypatch.setattr(SecureChannel, "recv_any", recording)
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    assert first == [State(relay=False, reader_ok=True)]
    await session.stop()


# -- identity is the key; hello.id is fixed at pairing ------------------------------


async def test_hello_id_of_another_device_is_rejected(harness: Harness) -> None:
    victim = await harness.paired_device(device_id="AAAA0001", relay_allowed=True)
    rogue = await harness.paired_device(device_id="BBBB0002")
    live = await harness.start(victim).connected()
    rogue.device_id = "AAAA0001"
    assert await harness.start(rogue).result() == 4001
    assert harness.server.get_session("AAAA0001") is harness.server.get_session_by_key(
        victim.public_key
    )
    assert not harness.server.is_connected("BBBB0002")
    ids = {d.public_key: d.device_id for d in await harness.storage.list_devices()}
    assert ids == {victim.public_key: "AAAA0001", rogue.public_key: "BBBB0002"}
    assert len(harness.of(DeviceConnected)) == 1
    result = await harness.server.send_relay("AAAA0001", "on")
    assert result.ok and victim.relay
    await harness.server.unpair("BBBB0002")
    assert await harness.storage.get_device(victim.public_key) is not None
    await live.stop()


async def test_changed_hello_id_is_rejected(harness: Harness) -> None:
    device = await harness.paired_device(device_id="C00F0202")
    device.device_id = "C00F0203"
    assert await harness.start(device).result() == 4001
    stored = await harness.storage.get_device(device.public_key)
    assert stored is not None and stored.device_id == "C00F0202"


async def test_get_session_by_key(harness: Harness) -> None:
    device = await harness.paired_device()
    assert harness.server.get_session_by_key(device.public_key) is None
    session = await harness.start(device).connected()
    by_key = harness.server.get_session_by_key(device.public_key)
    assert by_key is not None and by_key is harness.server.get_session(device.device_id)
    assert by_key.device.public_key == device.public_key
    await session.stop()


# -- device side: batches, epoch, relay -------------------------------------------------


async def test_epoch_change_with_batch_in_flight(harness: Harness) -> None:
    gate = asyncio.Event()
    batches: list[tuple[str, int, int, int]] = []
    original = harness.storage.store_punches

    async def gated(dev, punches, cursor):  # type: ignore[no-untyped-def]
        batches.append(
            (cursor.epoch, punches[0].index if punches else -1, len(punches), cursor.next)
        )
        if len(batches) == 1:
            await gate.wait()
        return await original(dev, punches, cursor)

    harness.storage.store_punches = gated  # type: ignore[method-assign]
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    old_epoch = device.log.epoch
    for i in range(3):
        device.punch(UID, 100 + i)
    await until(lambda: bool(batches))
    device.delete_record(0)
    new_epoch = device.log.epoch
    assert new_epoch != old_epoch
    await asyncio.sleep(0.05)
    assert len(batches) == 1  # waits for the ack of the batch in flight
    gate.set()
    await device.wait_delivered()
    assert batches == [(old_epoch, 0, 3, 3), (new_epoch, 1, 2, 3)]
    assert harness.storage.cursors[device.public_key] == Cursor(new_epoch, 3)
    assert len(harness.of(PunchReceived)) == 3  # the resent ones were duplicates
    await session.stop()


async def test_purge_during_session_restarts_from_zero(harness: Harness) -> None:
    device = await harness.paired_device()
    session = await harness.start(device).connected()
    device.punch(UID, 1)
    await device.wait_delivered()
    device.purge_log()
    await device.wait_delivered()
    assert device.acked == Cursor(device.log.epoch, 0)
    device.punch(UID, 2)
    await device.wait_delivered()
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 1)
    assert [(p.epoch, p.index) for p in harness.storage.punches][-1] == (device.log.epoch, 0)
    await session.stop()


async def test_device_sends_empty_batch_for_skipped_records(harness: Harness) -> None:
    sizes: list[int] = []
    original = harness.storage.store_punches

    async def counting(dev, punches, cursor):  # type: ignore[no-untyped-def]
        sizes.append(len(punches))
        return await original(dev, punches, cursor)

    harness.storage.store_punches = counting  # type: ignore[method-assign]
    device = await harness.paired_device()
    for i in range(3):
        device.punch(UID, 100 + i)
    device.log._records[:] = [None, None, None]  # damaged records, epoch unchanged
    session = await harness.start(device).connected()
    await device.wait_delivered()
    assert sizes == [0]
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 3)
    await session.stop()


def test_batch_fits_one_noise_message() -> None:
    class BigLog(EventLog):
        base = 4_000_000_000

        @property
        def count(self) -> int:
            return self.base + 300

        def records_from(self, index: int) -> Iterator[Punch]:
            for i in range(max(index, self.base), self.count):
                yield Punch(i, "04A1B2C3D4E5F6", 4294967295)

    device = LinkDevice(log=BigLog("0badc0de"))
    batch = device._build_batch(BigLog.base)
    data = encode(batch)
    assert len(data) + 16 <= 4096
    assert 0 < len(batch.items) < 100
    assert batch.next == batch.items[-1].index + 1
    assert decode(data) == batch
    # Without the size limit the next record would not have fit.
    item = f'[{batch.next},"04A1B2C3D4E5F6",4294967295]'
    assert len(data) + 1 + len(item) + 16 > 4096


async def test_on_cancels_pulse_in_progress(harness: Harness) -> None:
    device = await harness.paired_device(relay_allowed=True, open_ms=100)
    session = await harness.start(device).connected()
    assert (await harness.server.send_relay(device.device_id, "open")).ok
    assert (await harness.server.send_relay(device.device_id, "on")).ok
    await asyncio.sleep(0.2)
    assert device.relay  # the pulse did not switch it off
    await session.stop()


async def test_relay_disabled_locally_is_not_allowed(harness: Harness) -> None:
    device = await harness.paired_device(relay_allowed=True, relay_enabled=False)
    session = await harness.start(device).connected()
    for cmd in ("open", "on", "off"):
        result = await harness.server.send_relay(device.device_id, cmd)
        assert not result.ok and result.err == "not_allowed"
    assert not device.relay
    await session.stop()


async def test_revoking_consent_returns_relay_to_rest(harness: Harness) -> None:
    device = await harness.paired_device(relay_allowed=True)
    session = await harness.start(device).connected()
    assert (await harness.server.send_relay(device.device_id, "on")).ok
    await until(lambda: any(e.relay for e in harness.of(StateChanged)))
    await device.set_relay_allowed(False)
    assert not device.relay
    await until(lambda: not harness.of(StateChanged)[-1].relay)
    result = await harness.server.send_relay(device.device_id, "on")
    assert result.err == "not_allowed"
    await session.stop()


async def test_unpair_returns_relay_to_rest(harness: Harness) -> None:
    device = await harness.paired_device(relay_allowed=True)
    session = await harness.start(device).connected()
    assert (await harness.server.send_relay(device.device_id, "on")).ok
    await harness.server.unpair(device.device_id)
    assert await session.result() == 1000
    assert not device.relay and not device.is_paired
