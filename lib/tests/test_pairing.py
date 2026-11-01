from __future__ import annotations

import asyncio
from typing import Any

import pytest

from adaraz import (
    Cursor,
    DevicePaired,
    InvalidCodeError,
    PairingCancelled,
    PairingExpired,
    PairingFailed,
    PairingRejected,
    derive_psk,
    generate_code,
    memory_pipe,
)
from adaraz.const import PROLOGUE_PAIR
from adaraz.device import EventLog, LinkDevice
from adaraz.messages import encode
from adaraz.noise import XXPSK3, HandshakeState, KeyPair

from .conftest import Harness


async def test_pairing_success(harness: Harness) -> None:
    log = EventLog()
    for i in range(3):
        log.append("04A1B2C3", 1_700_000_000 + i)
    device = LinkDevice(device_id="C00F0202", name="Wejscie", log=log)
    window = harness.server.open_pairing()
    assert len(window.code) == 14 and window.code[4] == window.code[9] == "-"
    info = await harness.pair(device, window.code.lower())
    paired = await window

    assert paired.public_key == device.public_key
    assert paired.device_id == "C00F0202"
    assert paired.name == "Wejscie"
    assert paired.model == "RAZ-RCP-1"
    assert device.server_public_key == harness.server.public_key
    assert info.server_id == harness.server.server_id
    assert len(info.server_id) == 32
    assert info.server == "Test" and info.name == "Dom"
    assert await harness.storage.get_device(device.public_key) == paired
    # import_history (default): the first session starts at index 0.
    assert harness.storage.cursors[device.public_key] == Cursor(log.epoch, 0)
    assert harness.of(DevicePaired) == [DevicePaired(paired)]
    assert not window.active
    assert harness.server.pairing_windows == []


async def test_pairing_without_history(harness: Harness) -> None:
    device = LinkDevice()
    for _ in range(5):
        device.punch("04A1B2C3")
    window = harness.server.open_pairing(import_history=False)
    await harness.pair(device, window.code)
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 5)


async def test_wrong_code(harness: Harness) -> None:
    window = harness.server.open_pairing()
    device = LinkDevice()
    with pytest.raises(PairingRejected) as err:
        await harness.pair(device, generate_code())
    assert err.value.close_code == 4003
    assert not device.is_paired
    assert window.failures == 1 and window.active
    assert await harness.storage.list_devices() == []
    # The right code still works.
    await harness.pair(device, window.code)
    assert (await window).public_key == device.public_key


async def test_five_failures_close_window(harness: Harness) -> None:
    window = harness.server.open_pairing()
    for _ in range(5):
        with pytest.raises(PairingRejected):
            await harness.pair(LinkDevice(), generate_code())
    with pytest.raises(PairingFailed):
        await window
    with pytest.raises(PairingRejected) as err:
        await harness.pair(LinkDevice(), window.code)
    assert err.value.close_code == 4003


async def test_expired_code(harness: Harness) -> None:
    window = harness.server.open_pairing(timeout=0.05)
    await asyncio.sleep(0.1)
    with pytest.raises(PairingExpired):
        await window
    with pytest.raises(PairingRejected) as err:
        await harness.pair(LinkDevice(), window.code)
    assert err.value.close_code == 4003


async def test_timeout_is_capped_at_ten_minutes(harness: Harness) -> None:
    import time

    window = harness.server.open_pairing(timeout=3600)
    assert window.expires_at <= time.time() + 600.5
    window.cancel()


async def test_single_use(harness: Harness) -> None:
    window = harness.server.open_pairing()
    await harness.pair(LinkDevice(device_id="00000001"), window.code)
    with pytest.raises(PairingRejected) as err:
        await harness.pair(LinkDevice(device_id="00000002"), window.code)
    assert err.value.close_code == 4003
    assert len(await harness.storage.list_devices()) == 1


async def test_no_window(harness: Harness) -> None:
    with pytest.raises(PairingRejected) as err:
        await harness.pair(LinkDevice(), generate_code())
    assert err.value.close_code == 4003


async def test_cancel(harness: Harness) -> None:
    window = harness.server.open_pairing()
    window.cancel()
    with pytest.raises(PairingCancelled):
        await window.wait()
    with pytest.raises(PairingRejected):
        await harness.pair(LinkDevice(), window.code)


async def test_two_windows(harness: Harness) -> None:
    first = harness.server.open_pairing()
    second = harness.server.open_pairing()
    device = LinkDevice()
    await harness.pair(device, second.code)
    assert (await second).public_key == device.public_key
    assert first.active


async def test_invalid_code_rejected_locally(harness: Harness) -> None:
    dev_end, _ = memory_pipe()
    with pytest.raises(InvalidCodeError):
        await LinkDevice().pair(dev_end, "ABCD-EFGH-12U4")
    assert dev_end.sent == []


async def test_repair_after_factory_reset_replaces_device(harness: Harness) -> None:
    old = await harness.paired_device(device_id="C00F0202")
    new = await harness.paired_device(device_id="C00F0202")
    devices = await harness.storage.list_devices()
    assert [d.public_key for d in devices] == [new.public_key]
    assert old.public_key not in harness.storage.cursors


async def test_repair_same_key_keeps_cursor(harness: Harness) -> None:
    device = await harness.paired_device()
    harness.storage.cursors[device.public_key] = Cursor(device.log.epoch, 0)
    for _ in range(3):
        device.punch("04A1B2C3")
    session = await harness.start(device).connected()
    await device.wait_delivered()
    await session.stop()
    window = harness.server.open_pairing(import_history=True)
    await harness.pair(device, window.code)
    assert harness.storage.cursors[device.public_key] == Cursor(device.log.epoch, 3)


async def test_bad_first_message(harness: Harness) -> None:
    dev_end, srv_end = memory_pipe()
    task = harness.serve(srv_end)
    await dev_end.send(b"X" + bytes(48))
    await task
    assert dev_end.close_code == 4000


async def test_text_first_message(harness: Harness) -> None:
    dev_end, srv_end = memory_pipe()
    task = harness.serve(srv_end)
    await dev_end.send_text("P")
    await task
    assert dev_end.close_code == 4000


async def test_oversized_first_message(harness: Harness) -> None:
    harness.server.open_pairing()
    dev_end, srv_end = memory_pipe()
    task = harness.serve(srv_end)
    await dev_end.send(b"P" + bytes(4097))
    await task
    assert dev_end.close_code == 4000


# -- failed attempts are only counted for handshakes that reached the code check ----


async def _pairing_to_message2(harness: Harness, code: str) -> tuple[HandshakeState, Any, Any]:
    dev_end, srv_end = memory_pipe()
    task = harness.serve(srv_end)
    hs = HandshakeState(
        XXPSK3, True, prologue=PROLOGUE_PAIR, s=KeyPair.generate(), psk=derive_psk(code)
    )
    await dev_end.send(b"P" + hs.write_message(b""))
    data = await dev_end.recv()
    assert data is not None
    hs.read_message(data)
    return hs, dev_end, task


@pytest.mark.parametrize("junk", [bytes(80), bytes(10), b""])
async def test_junk_message3_is_not_counted(harness: Harness, junk: bytes) -> None:
    window = harness.server.open_pairing()
    for _ in range(6):
        _, dev_end, task = await _pairing_to_message2(harness, generate_code())
        await dev_end.send(junk)
        async with asyncio.timeout(2):
            await task
        assert dev_end.close_code == 4000
    assert window.failures == 0 and window.active
    await harness.pair(LinkDevice(), window.code)
    assert (await window).device_id == "C00F0202"


async def test_dropped_handshakes_are_not_counted(harness: Harness) -> None:
    window = harness.server.open_pairing()
    for _ in range(6):
        _, dev_end, task = await _pairing_to_message2(harness, generate_code())
        await dev_end.close()
        async with asyncio.timeout(2):
            await task
    assert window.failures == 0 and window.active


async def test_wrong_code_counts_against_every_open_window(harness: Harness) -> None:
    first = harness.server.open_pairing()
    second = harness.server.open_pairing()
    hs, dev_end, task = await _pairing_to_message2(harness, generate_code())
    await dev_end.send(hs.write_message(encode(LinkDevice().hello())))
    async with asyncio.timeout(2):
        await task
    assert dev_end.close_code == 4003
    assert first.failures == second.failures == 1


async def test_twelve_symbol_code_without_dashes(harness: Harness) -> None:
    window = harness.server.open_pairing()
    device = LinkDevice()
    await harness.pair(device, window.code.replace("-", " ").lower())
    assert (await window).public_key == device.public_key
