"""Persistent ADARAZ Link storage on top of Home Assistant's storage helper."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from adaraz import (
    Cursor,
    LinkStorage,
    PairedDevice,
    ProtocolError,
    PunchRecord,
    generate_private_key,
)
from adaraz.const import CLOSE_INTERNAL_ERROR
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEDUP_SIZE, STORAGE_KEY_DEVICE, STORAGE_KEY_SERVER, STORAGE_VERSION

type SeenKey = tuple[str, int]


def key_fingerprint(public_key: bytes) -> str:
    """Short, file name safe identifier of a device public key."""
    return public_key[:16].hex()


class StorageWriteError(ProtocolError):
    """A store could not be written to disk.

    It is a :class:`adaraz.ProtocolError` with close code 1011, so the link server
    closes the connection without acknowledging anything; the device keeps its data
    and sends it again later.
    """

    def __init__(self, key: str, reason: str) -> None:
        """Initialize."""
        super().__init__(f"could not write {key}: {reason}", CLOSE_INTERNAL_ERROR)


class DurableStore(Store[dict[str, Any]]):
    """A private, atomically written store whose save raises if nothing was written.

    ``Store.async_save`` logs serialization and write errors (e.g. a full disk) and
    returns normally, and while Home Assistant stops it only schedules the write for
    the final write stage. The link server acknowledges a batch only after it is
    stored durably (protocol section 6), so it needs to know.
    """

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        """Initialize a private store with atomic writes."""
        super().__init__(hass, STORAGE_VERSION, key, private=True, atomic_writes=True)
        self._save_lock = asyncio.Lock()
        self._written = False
        self._write_error: Exception | None = None

    async def _async_write_data(self, data: dict[str, Any]) -> None:
        # Called by Store under its write lock for every write that happens.
        try:
            await super()._async_write_data(data)
        except Exception as err:
            self._write_error = err
            raise
        self._written = True

    async def async_save_durable(self, data: dict[str, Any]) -> None:
        """Write ``data`` now; raise :class:`StorageWriteError` if it was not written.

        All writes of this store go through here, one at a time, so a save is never
        merged into another one.
        """
        async with self._save_lock:
            if self.hass.state is CoreState.stopping:
                raise StorageWriteError(self.key, "Home Assistant is stopping")
            self._written = False
            self._write_error = None
            await self.async_save(data)
            if not self._written:
                raise StorageWriteError(
                    self.key, str(self._write_error or "not written")
                ) from self._write_error


@dataclass(slots=True)
class DeviceLog:
    """Delivery state of one device.

    Deduplication (protocol section 6, key ``(uid, ts)`` per device):

    * A punch is a duplicate only if its exact key is in ``seen``, the keys of the
      ``DEDUP_SIZE`` newest stored punches. In the normal cursor flow nothing is
      ever dropped for its time stamp alone.
    * When the epoch changes, the device sends its whole log again from index 0.
      ``replay_ts`` is then the newest ``ts`` stored before the change: in the new
      epoch, punches not in ``seen`` with ``ts <= replay_ts`` are history that was
      (most likely) reported before. They are stored and acknowledged but not
      reported again.
    """

    store: DurableStore
    cursor: Cursor | None = None
    #: Recently stored punches as ``(uid, ts)``.
    seen: set[SeenKey] = field(default_factory=set)
    #: Newest ``ts`` ever stored.
    newest: int | None = None
    #: Newest ``ts`` stored before the last epoch change (None: no change yet).
    replay_ts: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the store."""
        return {
            "cursor": None if self.cursor is None else self.cursor.to_json(),
            "seen": sorted([uid, ts] for uid, ts in self.seen),
            "newest": self.newest,
            "replay_ts": self.replay_ts,
        }

    @classmethod
    def from_dict(cls, store: DurableStore, data: dict[str, Any]) -> DeviceLog:
        """Restore from the store (also from the format with a ``floor``)."""
        log = cls(store)
        if data.get("cursor") is not None:
            log.cursor = Cursor.from_json(data["cursor"])
        log.seen = {(str(uid), int(ts)) for uid, ts in data.get("seen", [])}
        candidates = [ts for _, ts in log.seen]
        for name in ("newest", "floor"):
            if (value := data.get(name)) is not None:
                candidates.append(int(value))
        log.newest = max(candidates, default=None)
        if (replay_ts := data.get("replay_ts")) is not None:
            log.replay_ts = int(replay_ts)
        return log


def _bounded(seen: set[SeenKey]) -> set[SeenKey]:
    """Keep the ``DEDUP_SIZE`` newest keys (by ``ts``)."""
    if len(seen) <= DEDUP_SIZE:
        return seen
    return set(sorted(seen, key=lambda key: key[1])[-DEDUP_SIZE:])


class AdarazStorage(LinkStorage):
    """:class:`adaraz.LinkStorage` kept in Home Assistant's ``.storage`` directory.

    The server key and the paired devices are in one store. Each device has its own
    store with the delivery cursor and the keys of recent punches, so that the
    cursor and the deduplication data are written together, atomically, before the
    server acknowledges a batch. All stores are private and every write raises
    :class:`StorageWriteError` if it did not reach the disk.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the storage."""
        self._hass = hass
        self._store = DurableStore(hass, STORAGE_KEY_SERVER)
        self._private_key = b""
        self._devices: dict[bytes, PairedDevice] = {}
        self._logs: dict[bytes, DeviceLog] = {}
        self._lock = asyncio.Lock()
        #: Called before a batch of punches is stored; may raise to refuse the batch
        #: (the device then retries later, nothing is acknowledged).
        self.before_store: Callable[[PairedDevice], None] | None = None

    @property
    def private_key(self) -> bytes:
        """Server static private key."""
        return self._private_key

    async def async_load(self) -> None:
        """Load the server store; generate and save the server key on first use."""
        data = await self._store.async_load()
        if data is None:
            self._private_key = generate_private_key()
            await self._async_save_server()
            return
        self._private_key = bytes.fromhex(data["private_key"])
        self._devices = {}
        for item in data.get("devices", []):
            device = PairedDevice.from_dict(item)
            self._devices[device.public_key] = device

    async def _async_save_server(self) -> None:
        await self._store.async_save_durable(
            {
                "private_key": self._private_key.hex(),
                "devices": [device.to_dict() for device in self._devices.values()],
            }
        )

    async def _async_log(self, public_key: bytes) -> DeviceLog:
        if (log := self._logs.get(public_key)) is not None:
            return log
        store = DurableStore(
            self._hass, STORAGE_KEY_DEVICE.format(key_fingerprint(public_key))
        )
        if (data := await store.async_load()) is not None:
            log = DeviceLog.from_dict(store, data)
        else:
            log = DeviceLog(store)
        # Another task may have loaded it meanwhile; keep the first one.
        return self._logs.setdefault(public_key, log)

    # -- LinkStorage -------------------------------------------------------------

    async def list_devices(self) -> Sequence[PairedDevice]:
        """Return all paired devices."""
        return list(self._devices.values())

    async def get_device(self, public_key: bytes) -> PairedDevice | None:
        """Return the paired device with this public key."""
        return self._devices.get(public_key)

    async def add_device(self, device: PairedDevice) -> None:
        """Insert or replace a paired device."""
        async with self._lock:
            previous = self._devices.get(device.public_key)
            self._devices[device.public_key] = device
            try:
                await self._async_save_server()
            except BaseException:
                if previous is None:
                    del self._devices[device.public_key]
                else:
                    self._devices[device.public_key] = previous
                raise

    async def remove_device(self, public_key: bytes) -> None:
        """Forget a device, its cursor and its seen punches."""
        async with self._lock:
            removed = self._devices.pop(public_key, None)
            if removed is not None:
                await self._async_save_server()
        log = await self._async_log(public_key)
        self._logs.pop(public_key, None)
        await log.store.async_remove()

    async def load_cursor(self, public_key: bytes) -> Cursor | None:
        """Return the stored cursor of a device."""
        return (await self._async_log(public_key)).cursor

    async def save_cursor(self, public_key: bytes, cursor: Cursor) -> None:
        """Store the cursor of a device."""
        log = await self._async_log(public_key)
        previous = log.cursor
        log.cursor = cursor
        try:
            await log.store.async_save_durable(log.as_dict())
        except BaseException:
            log.cursor = previous
            raise

    async def store_punches(
        self, device: PairedDevice, punches: Sequence[PunchRecord], cursor: Cursor
    ) -> Sequence[PunchRecord]:
        """Store the keys of new punches and the cursor; return the punches to report.

        See :class:`DeviceLog` for what is reported.
        """
        if self.before_store is not None:
            self.before_store(device)
        log = await self._async_log(device.public_key)
        replay_ts = log.replay_ts
        if log.cursor is not None and cursor.epoch != log.cursor.epoch:
            # The device sends its log again from index 0 (protocol section 6).
            replay_ts = log.newest
        new: list[PunchRecord] = []
        seen = set(log.seen)
        newest = log.newest
        for punch in punches:
            key = (punch.uid, punch.ts)
            if key in seen:
                continue
            seen.add(key)
            newest = punch.ts if newest is None else max(newest, punch.ts)
            if replay_ts is not None and punch.ts <= replay_ts:
                continue
            new.append(punch)
        previous = (log.cursor, log.seen, log.newest, log.replay_ts)
        log.cursor, log.seen = cursor, _bounded(seen)
        log.newest, log.replay_ts = newest, replay_ts
        try:
            await log.store.async_save_durable(log.as_dict())
        except BaseException:
            # Nothing is acknowledged; the device sends the batch again.
            log.cursor, log.seen, log.newest, log.replay_ts = previous
            raise
        return new

    # -- helpers ----------------------------------------------------------------------

    async def async_get_log(self, public_key: bytes) -> DeviceLog:
        """Return the delivery state of a device (for diagnostics)."""
        return await self._async_log(public_key)
