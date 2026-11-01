"""Binary sensors of ADARAZ devices: connection and card reader problem."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AdarazConfigEntry
from .entity import AdarazEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdarazConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    device = entry.runtime_data
    async_add_entities(
        [
            AdarazConnectionSensor(device, "connection"),
            AdarazReaderProblemSensor(device, "reader"),
        ]
    )


class AdarazConnectionSensor(AdarazEntity, BinarySensorEntity):
    """On while the device has a session with Home Assistant."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        """Return True if the device is connected."""
        return self._device.connected


class AdarazReaderProblemSensor(AdarazEntity, BinarySensorEntity):
    """On when the device reports that its card reader does not work."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Known only while connected and after the device reported its state."""
        return self._device.connected and self._device.state is not None

    @property
    def is_on(self) -> bool | None:
        """Return True if the card reader has a problem."""
        state = self._device.state
        return None if state is None else not state.reader_ok
