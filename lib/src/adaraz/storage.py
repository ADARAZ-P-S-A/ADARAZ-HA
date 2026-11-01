"""Data model and the storage interface a server application implements."""

from __future__ import annotations

import abc
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .messages import Cursor

__all__ = [
    "InMemoryStorage",
    "LinkStorage",
    "PairedDevice",
    "PunchDeduplicator",
    "PunchKey",
    "PunchRecord",
]

#: Deduplication key of a punch: ``(device id, uid, ts)`` (protocol section 6).
type PunchKey = tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class PairedDevice:
    """A device paired with this server. ``public_key`` is its identity."""

    public_key: bytes
    device_id: str
    name: str = ""
    model: str = ""
    fw: str = ""
    paired_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON friendly representation."""
        return {
            "public_key": self.public_key.hex(),
            "id": self.device_id,
            "name": self.name,
            "model": self.model,
            "fw": self.fw,
            "paired_at": self.paired_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PairedDevice:
        return cls(
            public_key=bytes.fromhex(data["public_key"]),
            device_id=data["id"],
            name=data.get("name", ""),
            model=data.get("model", ""),
            fw=data.get("fw", ""),
            paired_at=float(data.get("paired_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class PunchRecord:
    """A punch as received by the server."""

    device_id: str
    epoch: str
    index: int
    uid: str
    ts: int
    #: The punch arrived more than 5 minutes after ``ts``: keep it in history, but do
    #: not trigger real-time actions.
    late: bool
    #: Unix time of arrival at the server.
    received_at: float

    @property
    def key(self) -> PunchKey:
        return (self.device_id, self.uid, self.ts)


class PunchDeduplicator:
    """Set of seen :data:`PunchKey` values, optionally bounded (oldest dropped first)."""

    def __init__(self, keys: Iterable[PunchKey] = (), *, max_size: int | None = None) -> None:
        self._keys: OrderedDict[PunchKey, None] = OrderedDict()
        self._max_size = max_size
        for key in keys:
            self.add(key)

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: PunchKey) -> bool:
        """Record ``key``. Return ``True`` if it was not seen before."""
        if key in self._keys:
            return False
        self._keys[key] = None
        if self._max_size is not None and len(self._keys) > self._max_size:
            self._keys.popitem(last=False)
        return True

    def filter_new(self, punches: Iterable[PunchRecord]) -> list[PunchRecord]:
        """Return the punches not seen before and record them."""
        return [p for p in punches if self.add(p.key)]

    def discard_device(self, device_id: str) -> None:
        for key in [k for k in self._keys if k[0] == device_id]:
            del self._keys[key]


class LinkStorage(abc.ABC):
    """Persistence used by :class:`adaraz.LinkServer`.

    All methods are coroutines; implementations must not block the event loop (run
    file or database I/O in an executor). Devices are identified by their static
    public key (32 bytes).
    """

    @abc.abstractmethod
    async def list_devices(self) -> Sequence[PairedDevice]:
        """Return all paired devices."""

    async def get_device(self, public_key: bytes) -> PairedDevice | None:
        """Return the paired device with this static public key, or ``None``."""
        for device in await self.list_devices():
            if device.public_key == public_key:
                return device
        return None

    @abc.abstractmethod
    async def add_device(self, device: PairedDevice) -> None:
        """Insert or replace (by public key) a paired device. Must be durable."""

    @abc.abstractmethod
    async def remove_device(self, public_key: bytes) -> None:
        """Forget a device and its cursor. Stored punches may be kept."""

    @abc.abstractmethod
    async def load_cursor(self, public_key: bytes) -> Cursor | None:
        """Return the stored delivery cursor of a device, or ``None``."""

    @abc.abstractmethod
    async def save_cursor(self, public_key: bytes, cursor: Cursor) -> None:
        """Durably store the delivery cursor of a device."""

    @abc.abstractmethod
    async def store_punches(
        self, device: PairedDevice, punches: Sequence[PunchRecord], cursor: Cursor
    ) -> Sequence[PunchRecord]:
        """Durably store a batch of punches together with the new cursor.

        The server sends ``ack`` only after this returns, so the data must be on
        stable storage by then. ``punches`` may contain duplicates of punches stored
        earlier (key ``(device id, uid, ts)``, see :class:`PunchDeduplicator`); return
        the punches that are new. Only those are reported as
        :class:`adaraz.PunchReceived` events. ``punches`` may be empty (the batch only
        moves the cursor); the cursor must be stored anyway.
        """


class InMemoryStorage(LinkStorage):
    """Non-persistent :class:`LinkStorage`, for tests and examples."""

    def __init__(self) -> None:
        self.devices: dict[bytes, PairedDevice] = {}
        self.cursors: dict[bytes, Cursor] = {}
        self.punches: list[PunchRecord] = []
        self.dedup = PunchDeduplicator()

    async def list_devices(self) -> Sequence[PairedDevice]:
        return list(self.devices.values())

    async def get_device(self, public_key: bytes) -> PairedDevice | None:
        return self.devices.get(public_key)

    async def add_device(self, device: PairedDevice) -> None:
        self.devices[device.public_key] = device

    async def remove_device(self, public_key: bytes) -> None:
        self.devices.pop(public_key, None)
        self.cursors.pop(public_key, None)

    async def load_cursor(self, public_key: bytes) -> Cursor | None:
        return self.cursors.get(public_key)

    async def save_cursor(self, public_key: bytes, cursor: Cursor) -> None:
        self.cursors[public_key] = cursor

    async def store_punches(
        self, device: PairedDevice, punches: Sequence[PunchRecord], cursor: Cursor
    ) -> Sequence[PunchRecord]:
        new = self.dedup.filter_new(punches)
        self.punches.extend(new)
        self.cursors[device.public_key] = cursor
        return new
