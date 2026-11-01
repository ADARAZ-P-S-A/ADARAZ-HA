"""Protocol constants (ADARAZ Link protocol, version 1)."""

from __future__ import annotations

from typing import Final

PROTOCOL_VERSION: Final = 1
SUBPROTOCOL: Final = "adaraz.v1"
DEFAULT_PATH: Final = "/api/adaraz/ws"

MODE_PAIR: Final = 0x50  # "P"
MODE_SESSION: Final = 0x53  # "S"

PROLOGUE_PAIR: Final = b"ADARAZ/1 pair"
PROLOGUE_SESSION: Final = b"ADARAZ/1 session"

#: Largest Noise message (payload plus 16 byte tag).
MAX_NOISE_MESSAGE: Final = 4096
#: Largest JSON payload of a transport message.
MAX_PAYLOAD: Final = MAX_NOISE_MESSAGE - 16
#: Largest WebSocket message accepted at the WebSocket layer. Anything above
#: MAX_NOISE_MESSAGE (+1 for the mode byte) and below this is closed with 4000 by the
#: protocol code; anything above this is closed by the WebSocket library.
MAX_WS_MESSAGE: Final = 65536

MAX_PUNCH_ITEMS: Final = 100
#: Largest log index, count, cursor position and punch timestamp (uint32).
MAX_UINT32: Final = 0xFFFFFFFF

CLOSE_NORMAL: Final = 1000
CLOSE_GOING_AWAY: Final = 1001
CLOSE_INTERNAL_ERROR: Final = 1011
CLOSE_PROTOCOL_ERROR: Final = 4000
CLOSE_UNKNOWN_DEVICE: Final = 4001
CLOSE_SUPERSEDED: Final = 4002
CLOSE_PAIRING_REJECTED: Final = 4003

PING_INTERVAL: Final = 30.0
IDLE_TIMEOUT: Final = 90.0
#: A session that is not established (the server has not decrypted the device's first
#: transport message) within this time is closed (protocol section 4).
ESTABLISH_TIMEOUT: Final = 10.0
#: Time the server has to send ``paired`` after pairing message 3.
PAIRED_DEADLINE: Final = 10.0
#: Pairing window lifetime and attempt limit.
PAIRING_WINDOW: Final = 600.0
PAIRING_MAX_FAILURES: Final = 5
#: Punches older than this at arrival are "late".
LATE_AFTER: Final = 300.0

RELAY_COMMANDS: Final = ("open", "on", "off", "pulse")
PULSE_MS_MIN: Final = 100
PULSE_MS_MAX: Final = 10000

CAP_PUNCHES: Final = "punches"
CAP_RELAY: Final = "relay"
