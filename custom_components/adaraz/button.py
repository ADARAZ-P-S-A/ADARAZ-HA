"""Button of ADARAZ devices: open (pulse the relay like an accepted card)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AdarazConfigEntry
from .const import CAP_RELAY
from .entity import AdarazEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdarazConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button if the device allows remote relay control."""
    device = entry.runtime_data
    if CAP_RELAY in device.caps:
        async_add_entities([AdarazOpenButton(device, "open")])


class AdarazOpenButton(AdarazEntity, ButtonEntity):
    """Pulse the relay according to the device's relay settings."""

    @property
    def available(self) -> bool:
        """Available while the device is connected and allows relay control."""
        return self._device.relay_allowed

    async def async_press(self) -> None:
        """Send the ``open`` command."""
        await self._device.async_relay("open")
