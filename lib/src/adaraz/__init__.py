"""ADARAZ Link: secure connection between ADARAZ devices and servers.

See ``docs/protocol.md`` in the repository for the wire protocol.
"""

from __future__ import annotations

from .codes import derive_psk, format_code, generate_code, normalize_code
from .const import PROTOCOL_VERSION, RELAY_COMMANDS, SUBPROTOCOL
from .exceptions import (
    AdarazError,
    ConnectionClosed,
    DeviceNotConnected,
    DeviceNotFound,
    InvalidCodeError,
    PairingCancelled,
    PairingError,
    PairingExpired,
    PairingFailed,
    PairingRejected,
    ProtocolError,
    RelayTimeout,
)
from .messages import Cursor, Hello, RelayResult, State
from .noise import generate_private_key, public_key_from_private
from .server import (
    DeviceConnected,
    DeviceDisconnected,
    DevicePaired,
    DeviceSession,
    DeviceUnpaired,
    LinkEvent,
    LinkServer,
    PairingWindow,
    PunchReceived,
    StateChanged,
    server_id,
)
from .storage import (
    InMemoryStorage,
    LinkStorage,
    PairedDevice,
    PunchDeduplicator,
    PunchKey,
    PunchRecord,
)
from .transport import MemoryTransport, Transport, build_ws_url, memory_pipe

__version__ = "0.1.2"

__all__ = [
    "PROTOCOL_VERSION",
    "RELAY_COMMANDS",
    "SUBPROTOCOL",
    "AdarazError",
    "ConnectionClosed",
    "Cursor",
    "DeviceConnected",
    "DeviceDisconnected",
    "DeviceNotConnected",
    "DeviceNotFound",
    "DevicePaired",
    "DeviceSession",
    "DeviceUnpaired",
    "Hello",
    "InMemoryStorage",
    "InvalidCodeError",
    "LinkEvent",
    "LinkServer",
    "LinkStorage",
    "MemoryTransport",
    "PairedDevice",
    "PairingCancelled",
    "PairingError",
    "PairingExpired",
    "PairingFailed",
    "PairingRejected",
    "PairingWindow",
    "ProtocolError",
    "PunchDeduplicator",
    "PunchKey",
    "PunchReceived",
    "PunchRecord",
    "RelayResult",
    "RelayTimeout",
    "State",
    "StateChanged",
    "Transport",
    "__version__",
    "build_ws_url",
    "derive_psk",
    "format_code",
    "generate_code",
    "generate_private_key",
    "memory_pipe",
    "normalize_code",
    "public_key_from_private",
    "server_id",
]
