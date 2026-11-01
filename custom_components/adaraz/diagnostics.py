"""Diagnostics support for ADARAZ."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import AdarazConfigEntry
from .const import CONF_PUBLIC_KEY

TO_REDACT = {CONF_PUBLIC_KEY, "server_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AdarazConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    device = entry.runtime_data
    server = device.link.server
    log = await device.link.storage.async_get_log(device.public_key)
    stored = await device.link.storage.get_device(device.public_key)
    return async_redact_data(
        {
            "entry": {"title": entry.title, "data": dict(entry.data)},
            "device": None if stored is None else stored.to_dict(),
            "connected": device.connected,
            "hello": None if device.hello is None else device.hello.to_json(),
            "state": None if device.state is None else device.state.to_json(),
            "cursor": None if log.cursor is None else log.cursor.to_json(),
            "dedup": {
                "seen": len(log.seen),
                "newest": log.newest,
                "replay_ts": log.replay_ts,
            },
            "server": {
                "server_id": server.server_id,
                "sessions": len(server.sessions),
                "pairing_windows": len(server.pairing_windows),
            },
        },
        TO_REDACT,
    )
