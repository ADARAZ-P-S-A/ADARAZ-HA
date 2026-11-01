"""JSON message payloads (protocol section 5): encoding, decoding and validation.

Every Noise payload is one UTF-8 JSON object with a string field ``t``. Unknown fields
are ignored. An unknown ``t`` decodes to :class:`UnknownMessage`, which receivers
ignore outside the handshake.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .const import (
    MAX_PAYLOAD,
    MAX_PUNCH_ITEMS,
    MAX_UINT32,
    PROTOCOL_VERSION,
    PULSE_MS_MAX,
    PULSE_MS_MIN,
    RELAY_COMMANDS,
)
from .exceptions import ProtocolError

__all__ = [
    "DEVICE_TRANSPORT_TYPES",
    "SERVER_TRANSPORT_TYPES",
    "Ack",
    "Cursor",
    "Hello",
    "LogInfo",
    "Message",
    "PairHello",
    "Paired",
    "Ping",
    "Pong",
    "Punch",
    "Punches",
    "Relay",
    "RelayResult",
    "State",
    "UnknownMessage",
    "Unpair",
    "Welcome",
    "decode",
    "encode",
    "is_epoch",
]

_EPOCH_RE = re.compile(r"[0-9a-f]{8}")
_UID_RE = re.compile(r"(?:[0-9A-F]{2}){4,7}")
_SERVER_ID_RE = re.compile(r"[0-9a-f]{32}")
_MAX_TEXT = 256


def is_epoch(value: object) -> bool:
    """True if ``value`` is a valid log epoch (8 lowercase hex digits)."""
    return isinstance(value, str) and _EPOCH_RE.fullmatch(value) is not None


# -- field helpers -----------------------------------------------------------


def _get(data: dict[str, Any], key: str, t: str) -> Any:
    try:
        return data[key]
    except KeyError:
        raise ProtocolError(f"{t}: missing field {key!r}") from None


def _int(value: Any, what: str, *, minimum: int | None = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"{what}: integer expected")
    if minimum is not None and value < minimum:
        raise ProtocolError(f"{what}: must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ProtocolError(f"{what}: must be <= {maximum}")
    return value


def _u32(value: Any, what: str) -> int:
    """Log indices, counts, cursor positions and timestamps: 0 to 2^32 - 1."""
    return _int(value, what, minimum=0, maximum=MAX_UINT32)


def _opt_int(value: Any, what: str) -> int | None:
    return None if value is None else _int(value, what, minimum=None)


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{what}: boolean expected")
    return value


def _str(value: Any, what: str, *, max_len: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{what}: string expected")
    if len(value) > max_len:
        raise ProtocolError(f"{what}: string too long")
    return value


def _epoch(value: Any, what: str) -> str:
    if not is_epoch(value):
        raise ProtocolError(f"{what}: epoch must be 8 lowercase hex digits")
    return str(value)


# -- payload types -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cursor:
    """Position in a device event log: ``epoch`` and the next index to deliver."""

    epoch: str
    next: int

    def to_json(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next}

    @classmethod
    def from_json(cls, data: Any) -> Cursor:
        if not isinstance(data, dict):
            raise ProtocolError("cursor: object expected")
        return cls(
            _epoch(_get(data, "epoch", "cursor"), "cursor.epoch"),
            _u32(_get(data, "next", "cursor"), "cursor.next"),
        )


@dataclass(frozen=True, slots=True)
class LogInfo:
    """``log`` field of ``hello``."""

    epoch: str
    count: int


@dataclass(frozen=True, slots=True)
class Punch:
    """One record of a ``punches`` message: ``[index, uid_hex, unix_ts]``."""

    index: int
    uid: str
    ts: int


@dataclass(frozen=True, slots=True)
class Message:
    """Base class of all payloads."""

    T: ClassVar[str] = ""

    def to_json(self) -> dict[str, Any]:
        return {"t": self.T}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Message:
        return cls()


@dataclass(frozen=True, slots=True)
class UnknownMessage(Message):
    """A payload with a ``t`` this library does not know."""

    t: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {**self.data, "t": self.t}


@dataclass(frozen=True, slots=True)
class Hello(Message):
    """Device hello (pairing message 3 and session message 1)."""

    T: ClassVar[str] = "hello"
    id: str = ""
    log: LogInfo = LogInfo("00000000", 0)
    model: str = ""
    fw: str = ""
    name: str = ""
    caps: tuple[str, ...] = ("punches",)
    proto: int = PROTOCOL_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "proto": self.proto,
            "model": self.model,
            "fw": self.fw,
            "id": self.id,
            "name": self.name,
            "caps": list(self.caps),
            "log": {"epoch": self.log.epoch, "count": self.log.count},
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Hello:
        t = cls.T
        log = _get(data, "log", t)
        if not isinstance(log, dict):
            raise ProtocolError("hello.log: object expected")
        caps = data.get("caps", [])
        if not isinstance(caps, list) or len(caps) > 32:
            raise ProtocolError("hello.caps: list expected")
        device_id = _str(_get(data, "id", t), "hello.id", max_len=64)
        if not device_id:
            raise ProtocolError("hello.id: empty")
        return cls(
            proto=_int(_get(data, "proto", t), "hello.proto"),
            id=device_id,
            model=_str(data.get("model", ""), "hello.model"),
            fw=_str(data.get("fw", ""), "hello.fw"),
            name=_str(data.get("name", ""), "hello.name"),
            caps=tuple(_str(c, "hello.caps", max_len=64) for c in caps),
            log=LogInfo(
                _epoch(_get(log, "epoch", "hello.log"), "hello.log.epoch"),
                _u32(_get(log, "count", "hello.log"), "hello.log.count"),
            ),
        )


@dataclass(frozen=True, slots=True)
class Punches(Message):
    """A batch of event log records.

    ``items`` may be empty (a batch that only covers skipped indices). All integers
    are uint32 (0 to 2^32 - 1).
    """

    T: ClassVar[str] = "punches"
    epoch: str = "00000000"
    from_: int = 0
    items: tuple[Punch, ...] = ()
    next: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "epoch": self.epoch,
            "from": self.from_,
            "items": [[p.index, p.uid, p.ts] for p in self.items],
            "next": self.next,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Punches:
        t = cls.T
        epoch = _epoch(_get(data, "epoch", t), "punches.epoch")
        start = _u32(_get(data, "from", t), "punches.from")
        nxt = _u32(_get(data, "next", t), "punches.next")
        raw = _get(data, "items", t)
        if not isinstance(raw, list):
            raise ProtocolError("punches.items: list expected")
        if len(raw) > MAX_PUNCH_ITEMS:
            raise ProtocolError("punches.items: more than 100 items")
        if nxt < start:
            raise ProtocolError("punches.next is below punches.from")
        items: list[Punch] = []
        last = start - 1
        for item in raw:
            if not isinstance(item, list) or len(item) < 3:
                raise ProtocolError("punches.items: [index, uid, ts] expected")
            index = _u32(item[0], "punch index")
            uid = item[1]
            if not isinstance(uid, str) or _UID_RE.fullmatch(uid) is None:
                raise ProtocolError("punch uid: 4 to 7 bytes of uppercase hex expected")
            ts = _u32(item[2], "punch ts")
            if index <= last or index >= nxt:
                raise ProtocolError("punch indices must increase within [from, next)")
            last = index
            items.append(Punch(index, uid, ts))
        return cls(epoch=epoch, from_=start, items=tuple(items), next=nxt)


@dataclass(frozen=True, slots=True)
class State(Message):
    """Device state."""

    T: ClassVar[str] = "state"
    relay: bool = False
    reader_ok: bool = True

    def to_json(self) -> dict[str, Any]:
        return {"t": self.T, "relay": self.relay, "reader_ok": self.reader_ok}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> State:
        return cls(
            relay=_bool(_get(data, "relay", cls.T), "state.relay"),
            reader_ok=_bool(_get(data, "reader_ok", cls.T), "state.reader_ok"),
        )


@dataclass(frozen=True, slots=True)
class RelayResult(Message):
    """Answer to a ``relay`` command."""

    T: ClassVar[str] = "relay_result"
    id: int = 0
    ok: bool = False
    relay: bool | None = None
    err: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"t": self.T, "id": self.id, "ok": self.ok}
        if self.relay is not None:
            out["relay"] = self.relay
        if self.err is not None:
            out["err"] = self.err
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RelayResult:
        relay = data.get("relay")
        err = data.get("err")
        return cls(
            id=_int(_get(data, "id", cls.T), "relay_result.id", minimum=None),
            ok=_bool(_get(data, "ok", cls.T), "relay_result.ok"),
            relay=None if relay is None else _bool(relay, "relay_result.relay"),
            err=None if err is None else _str(err, "relay_result.err", max_len=64),
        )


@dataclass(frozen=True, slots=True)
class Ping(Message):
    T: ClassVar[str] = "ping"
    id: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"t": self.T} if self.id is None else {"t": self.T, "id": self.id}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Ping:
        return cls(id=_opt_int(data.get("id"), "ping.id"))


@dataclass(frozen=True, slots=True)
class Pong(Message):
    T: ClassVar[str] = "pong"
    id: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"t": self.T} if self.id is None else {"t": self.T, "id": self.id}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Pong:
        return cls(id=_opt_int(data.get("id"), "pong.id"))


@dataclass(frozen=True, slots=True)
class PairHello(Message):
    """Server hello in pairing message 2."""

    T: ClassVar[str] = "pair_hello"
    server: str = ""
    name: str = ""
    proto: int = PROTOCOL_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"t": self.T, "proto": self.proto, "server": self.server, "name": self.name}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PairHello:
        return cls(
            proto=_int(_get(data, "proto", cls.T), "pair_hello.proto"),
            server=_str(data.get("server", ""), "pair_hello.server"),
            name=_str(data.get("name", ""), "pair_hello.name"),
        )


@dataclass(frozen=True, slots=True)
class Paired(Message):
    """Pairing confirmation (first transport message after pairing)."""

    T: ClassVar[str] = "paired"
    server_id: str = ""
    name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"t": self.T, "server_id": self.server_id, "name": self.name}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Paired:
        server_id = _get(data, "server_id", cls.T)
        if not isinstance(server_id, str) or _SERVER_ID_RE.fullmatch(server_id) is None:
            raise ProtocolError("paired.server_id: 32 lowercase hex digits expected")
        return cls(server_id=server_id, name=_str(data.get("name", ""), "paired.name"))


@dataclass(frozen=True, slots=True)
class Welcome(Message):
    """Server answer in session message 2."""

    T: ClassVar[str] = "welcome"
    cursor: Cursor | None = None
    server: str = ""
    name: str = ""
    proto: int = PROTOCOL_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "t": self.T,
            "proto": self.proto,
            "server": self.server,
            "name": self.name,
            "cursor": None if self.cursor is None else self.cursor.to_json(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Welcome:
        cursor = _get(data, "cursor", cls.T)
        return cls(
            proto=_int(_get(data, "proto", cls.T), "welcome.proto"),
            server=_str(data.get("server", ""), "welcome.server"),
            name=_str(data.get("name", ""), "welcome.name"),
            cursor=None if cursor is None else Cursor.from_json(cursor),
        )


@dataclass(frozen=True, slots=True)
class Ack(Message):
    """The server stored everything below ``next`` of log ``epoch``."""

    T: ClassVar[str] = "ack"
    epoch: str = "00000000"
    next: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"t": self.T, "epoch": self.epoch, "next": self.next}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Ack:
        return cls(
            epoch=_epoch(_get(data, "epoch", cls.T), "ack.epoch"),
            next=_u32(_get(data, "next", cls.T), "ack.next"),
        )


@dataclass(frozen=True, slots=True)
class Relay(Message):
    """Relay command.

    Decoding only checks types: the device answers a bad ``cmd`` or ``ms`` with
    ``relay_result`` ``invalid`` instead of closing the connection. Use
    :meth:`validate` before sending.
    """

    T: ClassVar[str] = "relay"
    id: int = 0
    cmd: str = "open"
    ms: int | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"t": self.T, "id": self.id, "cmd": self.cmd}
        if self.ms is not None:
            out["ms"] = self.ms
        return out

    def is_valid(self) -> bool:
        if self.cmd not in RELAY_COMMANDS:
            return False
        if self.cmd == "pulse":
            return self.ms is not None and PULSE_MS_MIN <= self.ms <= PULSE_MS_MAX
        return True

    def validate(self) -> None:
        if not self.is_valid():
            raise ValueError(
                f"invalid relay command {self.cmd!r} (ms={self.ms}); cmd must be one of "
                f"{', '.join(RELAY_COMMANDS)} and pulse needs ms {PULSE_MS_MIN}-{PULSE_MS_MAX}"
            )

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Relay:
        ms = data.get("ms")
        return cls(
            id=_int(_get(data, "id", cls.T), "relay.id", minimum=None),
            cmd=_str(_get(data, "cmd", cls.T), "relay.cmd", max_len=32),
            ms=None if ms is None else _int(ms, "relay.ms", minimum=None),
        )


@dataclass(frozen=True, slots=True)
class Unpair(Message):
    T: ClassVar[str] = "unpair"


_TYPES: dict[str, type[Message]] = {
    cls.T: cls
    for cls in (
        Hello,
        Punches,
        State,
        RelayResult,
        Ping,
        Pong,
        PairHello,
        Paired,
        Welcome,
        Ack,
        Relay,
        Unpair,
    )
}

#: Types a server accepts from a device in transport messages.
DEVICE_TRANSPORT_TYPES: frozenset[type[Message]] = frozenset(
    {Punches, State, RelayResult, Ping, Pong}
)
#: Types a device accepts from a server in transport messages.
SERVER_TRANSPORT_TYPES: frozenset[type[Message]] = frozenset({Ack, Relay, Unpair, Ping, Pong})


def _reject_constant(value: str) -> Any:
    raise ProtocolError(f"invalid JSON constant {value}")


def encode(message: Message) -> bytes:
    """Serialise a message to compact UTF-8 JSON."""
    data = json.dumps(
        message.to_json(), separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(data) > MAX_PAYLOAD:
        raise ValueError(f"payload of {len(data)} bytes exceeds {MAX_PAYLOAD}")
    return data


def decode(payload: bytes) -> Message:
    """Parse and validate a payload. Raises :class:`ProtocolError` on bad input."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("payload is not UTF-8") from None
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ProtocolError("payload is not valid JSON") from None
    if not isinstance(data, dict):
        raise ProtocolError("payload is not a JSON object")
    t = data.get("t")
    if not isinstance(t, str):
        raise ProtocolError("payload has no string field 't'")
    cls = _TYPES.get(t)
    if cls is None:
        return UnknownMessage(t=t, data=data)
    return cls.from_json(data)
