"""Tests for the Home Assistant backed ADARAZ Link storage."""

from __future__ import annotations

from unittest.mock import patch

from adaraz import Cursor, PairedDevice, PunchRecord
import pytest

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.json import SerializationError
from homeassistant.util.file import WriteError

from custom_components.adaraz import storage as storage_module
from custom_components.adaraz.const import DOMAIN
from custom_components.adaraz.storage import (
    AdarazStorage,
    StorageWriteError,
    key_fingerprint,
)

DEVICE = PairedDevice(public_key=b"\x07" * 32, device_id="C00F0202")


def punch(index: int, uid: str, ts: int) -> PunchRecord:
    """Return a punch record of the test device."""
    return PunchRecord(
        device_id=DEVICE.device_id,
        epoch="0000000a",
        index=index,
        uid=uid,
        ts=ts,
        late=False,
        received_at=0.0,
    )


async def new_storage(hass: HomeAssistant) -> AdarazStorage:
    """Return a loaded storage."""
    storage = AdarazStorage(hass)
    await storage.async_load()
    await storage.add_device(DEVICE)
    return storage


async def test_deduplication_is_persistent(hass: HomeAssistant) -> None:
    """Seen punches are filtered, also by a new storage instance."""
    storage = await new_storage(hass)
    batch = [
        punch(0, "04000001", 100),
        punch(1, "04000002", 101),
        punch(2, "04000001", 100),
    ]
    new = await storage.store_punches(DEVICE, batch, Cursor("0000000a", 3))
    assert [p.index for p in new] == [0, 1]

    again = await new_storage(hass)
    assert await again.load_cursor(DEVICE.public_key) == Cursor("0000000a", 3)
    new = await again.store_punches(
        DEVICE,
        [punch(0, "04000001", 100), punch(3, "04000003", 102)],
        Cursor("0000000b", 4),
    )
    assert [p.uid for p in new] == ["04000003"]


async def test_bounded_without_floor(hass: HomeAssistant) -> None:
    """Only exact keys are duplicates: a new punch with an old ts is reported."""
    storage = await new_storage(hass)
    with patch.object(storage_module, "DEDUP_SIZE", 3):
        batch = [punch(i, f"0400000{i}", 100 + i) for i in range(5)]
        assert (
            len(await storage.store_punches(DEVICE, batch, Cursor("0000000a", 5))) == 5
        )
        log = await storage.async_get_log(DEVICE.public_key)
        assert log.seen == {("04000002", 102), ("04000003", 103), ("04000004", 104)}
        assert log.newest == 104
        # The clock went back (or a tie with an old punch): new keys are new.
        later = [
            punch(5, "04000000", 100),
            punch(6, "0400FFFF", 101),
            punch(7, "04000004", 104),
            punch(8, "0400EEEE", 50),
        ]
        new = await storage.store_punches(DEVICE, later, Cursor("0000000a", 9))
        assert [p.uid for p in new] == ["04000000", "0400FFFF", "0400EEEE"]
        assert log.replay_ts is None


async def test_epoch_change_replays_history_silently(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """After an epoch change, history up to the newest seen ts is not reported."""
    storage = await new_storage(hass)
    with patch.object(storage_module, "DEDUP_SIZE", 2):
        batch = [punch(i, f"0400000{i}", 100 + i) for i in range(4)]
        await storage.store_punches(DEVICE, batch, Cursor("0000000a", 4))
        log = await storage.async_get_log(DEVICE.public_key)
        assert log.seen == {("04000002", 102), ("04000003", 103)}

        # Record 0 deleted on the device: new epoch, everything again from 0, plus
        # a punch made meanwhile.
        replay = [
            punch(0, "04000001", 101),
            punch(1, "04000002", 102),
            punch(2, "04000003", 103),
        ]
        new = await storage.store_punches(DEVICE, replay, Cursor("0000000b", 3))
        assert new == []
        assert log.replay_ts == 103
        assert log.cursor == Cursor("0000000b", 3)
        # Next batch of the same epoch: still history up to 103, then new punches.
        rest = [punch(3, "04000009", 103), punch(4, "04000004", 104)]
        new = await storage.store_punches(DEVICE, rest, Cursor("0000000b", 5))
        assert [p.uid for p in new] == ["04000004"]

    # Persistent.
    again = AdarazStorage(hass)
    await again.async_load()
    restored = await again.async_get_log(DEVICE.public_key)
    assert (restored.newest, restored.replay_ts) == (104, 103)
    key = f"{DOMAIN}.device.{key_fingerprint(DEVICE.public_key)}"
    assert hass_storage[key]["data"]["replay_ts"] == 103


async def test_old_format_with_floor(hass: HomeAssistant, hass_storage: dict) -> None:
    """A store written with a dedup floor loads; the floor is not used any more."""
    key = f"{DOMAIN}.device.{key_fingerprint(DEVICE.public_key)}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {
            "cursor": {"epoch": "0000000a", "next": 3},
            "seen": [["04000003", 103]],
            "floor": 102,
        },
    }
    storage = await new_storage(hass)
    log = await storage.async_get_log(DEVICE.public_key)
    assert (log.newest, log.replay_ts) == (103, None)
    new = await storage.store_punches(
        DEVICE,
        [punch(3, "04000003", 103), punch(4, "04000001", 90)],
        Cursor("0000000a", 5),
    )
    assert [p.uid for p in new] == ["04000001"]
    assert "floor" not in hass_storage[key]["data"]


async def test_failed_write_is_rolled_back(hass: HomeAssistant) -> None:
    """If the write fails nothing is acknowledged and the punches stay new."""
    storage = await new_storage(hass)
    log = await storage.async_get_log(DEVICE.public_key)
    with (
        patch.object(log.store, "async_save", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        await storage.store_punches(
            DEVICE, [punch(0, "04000001", 100)], Cursor("0000000a", 1)
        )
    assert log.cursor is None
    assert not log.seen

    with (
        patch.object(log.store, "async_save", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        await storage.save_cursor(DEVICE.public_key, Cursor("0000000a", 1))
    assert log.cursor is None

    new = await storage.store_punches(
        DEVICE, [punch(0, "04000001", 100)], Cursor("0000000a", 1)
    )
    assert len(new) == 1


async def test_remove_device(hass: HomeAssistant, hass_storage: dict) -> None:
    """Removing a device deletes its store; removing an unknown one is harmless."""
    storage = await new_storage(hass)
    await storage.save_cursor(DEVICE.public_key, Cursor("0000000a", 0))
    key = f"{DOMAIN}.device.{key_fingerprint(DEVICE.public_key)}"
    assert key in hass_storage
    await storage.remove_device(DEVICE.public_key)
    assert key not in hass_storage
    assert await storage.list_devices() == []
    await storage.remove_device(b"\x09" * 32)


@pytest.mark.parametrize(
    "error", [WriteError("disk full"), SerializationError("bad value")]
)
async def test_write_error_is_raised(hass: HomeAssistant, error: Exception) -> None:
    """A write error that Store only logs is raised, so nothing is acknowledged."""
    storage = await new_storage(hass)
    log = await storage.async_get_log(DEVICE.public_key)
    with (
        patch(
            "homeassistant.helpers.storage.Store._async_write_data",
            side_effect=error,
        ),
        pytest.raises(StorageWriteError) as err,
    ):
        await storage.store_punches(
            DEVICE, [punch(0, "04000001", 100)], Cursor("0000000a", 1)
        )
    assert err.value.close_code == 1011
    assert log.cursor is None
    assert not log.seen

    other = PairedDevice(public_key=b"\x08" * 32, device_id="C00F0203")
    with (
        patch(
            "homeassistant.helpers.storage.Store._async_write_data",
            side_effect=error,
        ),
        pytest.raises(StorageWriteError),
    ):
        await storage.add_device(other)
    assert await storage.get_device(other.public_key) is None
    assert await storage.list_devices() == [DEVICE]


async def test_no_write_while_stopping(hass: HomeAssistant) -> None:
    """While Home Assistant stops, Store defers writes: that is an error here."""
    storage = await new_storage(hass)
    hass.set_state(CoreState.stopping)
    try:
        with pytest.raises(StorageWriteError):
            await storage.save_cursor(DEVICE.public_key, Cursor("0000000a", 1))
    finally:
        hass.set_state(CoreState.running)
    assert await storage.load_cursor(DEVICE.public_key) is None


async def test_stores_are_private(hass: HomeAssistant) -> None:
    """The server store and the device stores (card UIDs) are private."""
    storage = await new_storage(hass)
    await storage.save_cursor(DEVICE.public_key, Cursor("0000000a", 1))
    log = await storage.async_get_log(DEVICE.public_key)
    for store in (storage._store, log.store):
        # The test fixture keeps stores in memory; run the file write it replaces.
        with (
            patch("homeassistant.helpers.storage.os.makedirs"),
            patch("homeassistant.helpers.storage.write_utf8_file_atomic") as write,
        ):
            store._write_prepared_data("w", "{}")
        write.assert_called_once()
        assert write.call_args.args[2] is True  # private
