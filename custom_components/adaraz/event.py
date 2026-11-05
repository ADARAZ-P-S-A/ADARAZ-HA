"""Event entity of ADARAZ devices: card punches."""

from __future__ import annotations

from typing import Any

from adaraz import PunchRecord
from homeassistant.components.event import EventEntity, EventExtraStoredData
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AdarazConfigEntry
from .const import ATTR_CARD, ATTR_LATE, ATTR_TS, ATTR_UID
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

    ``card`` is the name an admin gave the card in the options, or else its card
    ID (``card_`` and 8 hex digits, see ``AdarazLink.card_id``). The raw card UID
    is a credential (many readers accept a cloned UID), so it is in the
    attributes only when the ``expose_uid`` option is on, it is never written to
    the recorder, and a restored state keeps it only while the option is on.

    ``late`` is true for punches that reached Home Assistant more than 5 minutes
    after they happened (the device was offline); automations that act in real
    time (e.g. open a door) should ignore those.
    """

    _attr_event_types = [EVENT_TYPE_PUNCH]
    _unrecorded_attributes = frozenset({ATTR_UID})

    async def async_added_to_hass(self) -> None:
        """Subscribe to punches."""
        await super().async_added_to_hass()
        self.async_on_remove(self._device.async_add_punch_listener(self._handle_punch))

    async def async_get_last_event_data(self) -> EventExtraStoredData | None:
        """Restore the last punch, without its UID unless the option is on.

        A state saved by an older version (or before the option was turned off)
        may hold the UID.
        """
        data = await super().async_get_last_event_data()
        if (
            data is not None
            and data.last_event_attributes
            and not self._device.expose_uid
        ):
            data.last_event_attributes = {
                name: value
                for name, value in data.last_event_attributes.items()
                if name != ATTR_UID
            }
        return data

    @callback
    def _handle_punch(self, punch: PunchRecord) -> None:
        attributes: dict[str, Any] = {
            ATTR_CARD: self._device.card_name(punch.uid),
            ATTR_TS: punch.ts,
            ATTR_LATE: punch.late,
        }
        if self._device.expose_uid:
            attributes[ATTR_UID] = punch.uid
        self._trigger_event(EVENT_TYPE_PUNCH, attributes)
        self.async_write_ha_state()
