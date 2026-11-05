"""Tests for the ADARAZ options flow: card names and the raw UID option."""

from __future__ import annotations

import time
from typing import Any

from adaraz.device import LinkDevice
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import DEVICE_ID, Harness, PairDevice, capture_punches
from custom_components.adaraz.config_flow import normalize_uid
from custom_components.adaraz.const import CONF_CARDS, CONF_EXPOSE_UID, DOMAIN

EVENT_ENTITY = "event.wejscie_punch"


async def open_step(hass: HomeAssistant, entry: ConfigEntry, step: str) -> Any:
    """Open the options flow and choose ``step`` in its menu."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert step in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


def uid_options(result: Any) -> list[dict[str, str]]:
    """Return the cards offered by the UID selector of the add_card form."""
    selector = result["data_schema"].schema["uid"]
    return list(selector.config["options"])


async def punch(device: LinkDevice, hass: HomeAssistant, uid: str, ts: int) -> None:
    """Punch a card and wait until Home Assistant has it."""
    device.punch(uid, ts=ts)
    await device.wait_delivered()
    await hass.async_block_till_done()


def test_normalize_uid() -> None:
    assert normalize_uid("04A1B2C3") == "04A1B2C3"
    assert normalize_uid(" 04:a1:b2:c3 ") == "04A1B2C3"
    assert normalize_uid("04-A1-B2-C3-D4-E5-F6") == "04A1B2C3D4E5F6"
    assert normalize_uid("04A1B2") is None  # 3 bytes
    assert normalize_uid("04A1B2C3D4E5F6A7") is None  # 8 bytes
    assert normalize_uid("04A1B2C") is None  # odd
    assert normalize_uid("04A1B2CG") is None


async def test_name_recent_card(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """A card that just punched is offered; its next punch has the name."""
    entry = await pair_device(device)
    events = capture_punches(hass, EVENT_ENTITY)
    now = int(time.time())
    await punch(device, hass, "04A1B2C3", now - 2)
    await punch(device, hass, "04FFEE11", now - 1)

    # Nothing to remove yet.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["add_card", "uid"]

    result = await open_step(hass, entry, "add_card")
    assert result["type"] is FlowResultType.FORM
    # Newest first, with their card IDs.
    link = entry.runtime_data.link
    assert uid_options(result)[0]["label"].startswith(link.card_id("04FFEE11"))
    assert [o["value"] for o in uid_options(result)] == ["04FFEE11", "04A1B2C3"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"uid": "04A1B2", "name": "Anna"}
    )
    assert result["errors"] == {"uid": "invalid_uid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"uid": "04A1B2C3", "name": " Card_3F9A2C71 "}
    )
    assert result["errors"] == {"name": "invalid_name"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"uid": "04:a1:b2:c3", "name": " Anna Kowalska "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_CARDS: {"04A1B2C3": "Anna Kowalska"}}
    await hass.async_block_till_done()
    assert list(entry.runtime_data.recent_unnamed) == ["04FFEE11"]

    await punch(device, hass, "04A1B2C3", now)
    assert events[-1] == {"card": "Anna Kowalska", "ts": now, "late": False}
    assert list(entry.runtime_data.recent_unnamed) == ["04FFEE11"]


async def test_name_typed_card_and_remove(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """A UID can be typed in; names can be removed again."""
    entry = await pair_device(device)
    result = await open_step(hass, entry, "add_card")
    assert "options" not in result["data_schema"].schema["uid"].config
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"uid": "04ffee11", "name": "Kurier"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await open_step(hass, entry, "add_card")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"uid": "04A1B2C3", "name": "Anna"}
    )
    assert entry.options == {CONF_CARDS: {"04A1B2C3": "Anna", "04FFEE11": "Kurier"}}

    result = await open_step(hass, entry, "remove_card")
    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema["cards"].config["options"]
    assert [o["label"] for o in options] == ["Anna (04A1B2C3)", "Kurier (04FFEE11)"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"cards": ["04FFEE11"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_CARDS: {"04A1B2C3": "Anna"}}

    events = capture_punches(hass, EVENT_ENTITY)
    now = int(time.time())
    await punch(device, hass, "04FFEE11", now)
    card_id = entry.runtime_data.link.card_id("04FFEE11")
    assert events == [{"card": card_id, "ts": now, "late": False}]


async def test_expose_uid_option(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """The UID option is off by default and reloads the entry when changed."""
    entry = await pair_device(device)
    result = await open_step(hass, entry, "uid")
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]({})
    assert schema == {CONF_EXPOSE_UID: False}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXPOSE_UID: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.runtime_data.expose_uid

    result = await open_step(hass, entry, "uid")
    assert result["data_schema"]({}) == {CONF_EXPOSE_UID: True}


async def test_options_of_entry_not_set_up(hass: HomeAssistant) -> None:
    """The options flow works for an entry that was never set up."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DEVICE_ID, data={})
    entry.add_to_hass(hass)
    result = await open_step(hass, entry, "add_card")
    assert "options" not in result["data_schema"].schema["uid"].config
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"uid": "04A1B2C3", "name": "Anna"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_CARDS: {"04A1B2C3": "Anna"}}


async def test_recent_unnamed_bounded(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """Only the 10 newest unnamed cards are offered; a card punching again moves up."""
    entry = await pair_device(device)
    now = int(time.time())
    for i in range(11):
        device.punch(f"040000{i:02X}", ts=now - 20 + i)
    device.punch("04000001", ts=now)
    await device.wait_delivered()
    await hass.async_block_till_done()
    recent = entry.runtime_data.recent_unnamed
    assert list(recent) == [f"040000{i:02X}" for i in (*range(2, 11), 1)]
    assert recent["04000001"] == now
