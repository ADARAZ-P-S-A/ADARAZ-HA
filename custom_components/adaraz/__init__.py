"""The ADARAZ integration.

ADARAZ devices (the RAZ-RCP-1 card reader / time clock) connect to Home Assistant
over a WebSocket at ``/api/adaraz/ws`` and talk the ADARAZ Link protocol: a Noise
session with mutual authentication, so plain ``ws://`` in a LAN is safe.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    CAP_RELAY,
    CONF_EXPOSE_UID,
    CONF_MODEL,
    CONF_NAME,
    DOMAIN,
    MANUFACTURER,
)
from .link import AdarazDevice, async_get_link

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.SWITCH,
]
#: Entities that exist only when the device allows remote relay control.
RELAY_ENTITIES = ((Platform.BUTTON, "open"), (Platform.SWITCH, "relay"))

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AdarazConfigEntry = ConfigEntry[AdarazDevice]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Start the link server (one per Home Assistant instance) and its endpoint."""
    await async_get_link(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AdarazConfigEntry) -> bool:
    """Set up an ADARAZ device."""
    link = await async_get_link(hass)
    # May update the entry's key (the device was paired again under a new key).
    stored = await link.async_ensure_device(entry)
    device = AdarazDevice(hass, entry, link)
    device.async_check_caps()

    registry_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.device_id)},
        manufacturer=MANUFACTURER,
        model=(device.hello.model if device.hello else None)
        or stored.model
        or entry.data.get(CONF_MODEL),
        name=stored.name or entry.data.get(CONF_NAME) or device.device_id,
        sw_version=(device.hello.fw if device.hello else None) or stored.fw or None,
        serial_number=device.device_id,
    )
    device.registry_id = registry_device.id

    if CAP_RELAY not in device.caps:
        entity_registry = er.async_get(hass)
        for platform, key in RELAY_ENTITIES:
            if entity_id := entity_registry.async_get_entity_id(
                platform, DOMAIN, f"{device.device_id}_{key}"
            ):
                entity_registry.async_remove(entity_id)

    entry.runtime_data = device
    link.devices[device.public_key] = device
    entry.async_on_unload(lambda: _async_forget(link.devices, device))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    device.ready = True
    return True


async def _async_options_updated(hass: HomeAssistant, entry: AdarazConfigEntry) -> None:
    """Reload when the UID option changed, so the restored state drops the UID.

    Card names apply to the next punch without a reload: during a reload punches are
    refused, and the device sends them again only after it reconnects.
    """
    if entry.runtime_data.expose_uid != bool(entry.options.get(CONF_EXPOSE_UID)):
        hass.config_entries.async_schedule_reload(entry.entry_id)


def _async_forget(devices: dict[bytes, AdarazDevice], device: AdarazDevice) -> None:
    if devices.get(device.public_key) is device:
        del devices[device.public_key]


async def async_unload_entry(hass: HomeAssistant, entry: AdarazConfigEntry) -> bool:
    """Unload a config entry. The device stays connected to the link server."""
    entry.runtime_data.ready = False
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: AdarazConfigEntry) -> None:
    """Unpair the device when its entry is deleted."""
    link = await async_get_link(hass)
    await link.async_unpair(entry)
