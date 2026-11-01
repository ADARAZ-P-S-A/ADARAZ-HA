"""Event entity of ADARAZ devices: card punches."""

from __future__ import annotations

from adaraz import PunchRecord
from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AdarazConfigEntry
from .const import ATTR_LATE, ATTR_TS, ATTR_UID
from .entity import AdarazEntity

PARALLEL_UPDATES = 0

EVENT_TYPE_PUNCH = "punch"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AdarazConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the punch event entity."""
    async_add_entities([AdarazPunchEvent(entry.runtime_data, "punch")])


class AdarazPunchEvent(AdarazEntity, EventEntity):
    """Fires for every new punch.

    ``late`` is true for punches that reached Home Assistant more than 5 minutes
    after they happened (the device was offline); automations that act in real
    time (e.g. open a door) should ignore those.
    """

    _attr_event_types = [EVENT_TYPE_PUNCH]

    async def async_added_to_hass(self) -> None:
        """Subscribe to punches."""
        await super().async_added_to_hass()
        self.async_on_remove(self._device.async_add_punch_listener(self._handle_punch))

    @callback
    def _handle_punch(self, punch: PunchRecord) -> None:
        self._trigger_event(
            EVENT_TYPE_PUNCH,
            {ATTR_UID: punch.uid, ATTR_TS: punch.ts, ATTR_LATE: punch.late},
        )
        self.async_write_ha_state()
