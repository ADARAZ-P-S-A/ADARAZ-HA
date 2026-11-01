"""Constants for the ADARAZ integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "adaraz"
MANUFACTURER: Final = "ADARAZ"

#: Path of the WebSocket endpoint devices connect to (protocol section 1).
WS_PATH: Final = "/api/adaraz/ws"
#: Software name sent to devices in ``pair_hello`` and ``welcome``.
SERVER_SOFTWARE: Final = "Home Assistant"

CONF_PUBLIC_KEY: Final = "public_key"
CONF_MODEL: Final = "model"
CONF_NAME: Final = "name"
CONF_CAPS: Final = "caps"

CAP_RELAY: Final = "relay"

ATTR_UID: Final = "uid"
ATTR_TS: Final = "ts"
ATTR_LATE: Final = "late"

STORAGE_VERSION: Final = 1
#: Server key and paired devices.
STORAGE_KEY_SERVER: Final = f"{DOMAIN}.server"
#: Per device delivery cursor and recently seen punches (suffix: key fingerprint).
#: Both stores are private: the device stores hold card UIDs with punch times.
STORAGE_KEY_DEVICE: Final = f"{DOMAIN}.device.{{}}"

#: Number of recent punch keys kept per device for deduplication.
DEDUP_SIZE: Final = 2000
#: Seconds to wait for the device's answer to a relay command.
RELAY_TIMEOUT: Final = 10.0
#: Seconds the config flow waits for the first session after pairing (to learn caps).
SESSION_WAIT: Final = 10.0
