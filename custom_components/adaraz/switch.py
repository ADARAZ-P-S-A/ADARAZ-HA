"""Switch of ADARAZ devices: the relay."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up the relay switch if the device allows remote relay control."""
    device = entry.runtime_data
    if CAP_RELAY in device.caps:
        async_add_entities([AdarazRelaySwitch(device, "relay")])


class AdarazRelaySwitch(AdarazEntity, SwitchEntity):
    """The relay; its state comes from the device's ``state`` messages."""

    @property
    def available(self) -> bool:
        """Available while the device is connected and allows relay control."""
        return self._device.relay_allowed

    @property
    def is_on(self) -> bool | None:
        """Return True if the relay is on."""
        state = self._device.state
        return None if state is None else state.relay

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the relay on."""
        await self._device.async_relay("on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the relay off."""
        await self._device.async_relay("off")
