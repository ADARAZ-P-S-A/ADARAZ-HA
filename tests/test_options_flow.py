"""Tests for the ADARAZ options flow: the raw card UID option."""

from __future__ import annotations

from adaraz.device import LinkDevice

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import Harness, PairDevice
from custom_components.adaraz.const import CONF_EXPOSE_UID


async def test_expose_uid_option(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """The UID option is off by default and reloads the entry when changed."""
    entry = await pair_device(device)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"]({}) == {CONF_EXPOSE_UID: False}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXPOSE_UID: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_EXPOSE_UID: True}
    assert entry.runtime_data.expose_uid

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({}) == {CONF_EXPOSE_UID: True}


async def test_card_names_of_0_1_4_dropped(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """Saving the options drops card names stored by 0.1.4, without a reload."""
    entry = await pair_device(device)
    hass.config_entries.async_update_entry(
        entry, options={"cards": {"04A1B2C3": "Anna"}}
    )
    await hass.async_block_till_done()
    runtime = entry.runtime_data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXPOSE_UID: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {CONF_EXPOSE_UID: False}
    assert entry.runtime_data is runtime
