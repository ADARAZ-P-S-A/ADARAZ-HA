"""Base entity of the ADARAZ integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .link import AdarazDevice


class AdarazEntity(Entity):
    """An entity of an ADARAZ device, updated by pushes from the device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, device: AdarazDevice, key: str) -> None:
        """Initialize the entity."""
        self._device = device
        self._attr_translation_key = key
        self._attr_unique_id = f"{device.device_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.device_id)})

    async def async_added_to_hass(self) -> None:
        """Subscribe to connection and state changes."""
        await super().async_added_to_hass()
        self.async_on_remove(self._device.async_add_listener(self.async_write_ha_state))
