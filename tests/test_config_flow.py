"""Tests for the ADARAZ config flow.

Every error path continues until the flow creates an entry or aborts. Results are
checked through the flow result, entity states and the registries, and on the
simulated reader (the peer), not through the integration's internals.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from adaraz import PairingRejected, normalize_code
from adaraz.device import LinkDevice
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util.file import WriteError

from .conftest import (
    DEVICE_ID,
    INTERNAL_URL,
    Harness,
    finish_flow,
    get_link,
    start_flow,
    until,
)
from custom_components.adaraz.const import (
    CONF_CAPS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PUBLIC_KEY,
    DOMAIN,
)

CONNECTION = "binary_sensor.wejscie_connection"
RELAY = "switch.wejscie_relay"


def entry_data(public_key: bytes, caps: list[str]) -> dict[str, Any]:
    """Return entry data of the test device."""
    return {
        CONF_PUBLIC_KEY: public_key.hex(),
        CONF_MODEL: "RAZ-RCP-1",
        CONF_NAME: "Wejscie",
        CONF_CAPS: caps,
    }


async def retry_and_pair(
    hass: HomeAssistant, harness: Harness, device: LinkDevice, result: dict[str, Any]
) -> dict[str, Any]:
    """Submit the retry form, pair with the new code and return the final result."""
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "retry"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    await harness.pair(device, result["description_placeholders"]["code"])
    await harness.start_session(device)
    result = await finish_flow(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == DEVICE_ID
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTION).state == STATE_ON
    return result


async def test_pairing_success(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A device pairs with the shown code and an entry is created."""
    result = await start_flow(hass)
    placeholders = result["description_placeholders"]
    assert placeholders["url"] == INTERNAL_URL
    assert placeholders["minutes"] == "10"
    code = placeholders["code"]
    assert len(normalize_code(code)) == 12
    assert [len(group) for group in code.split("-")] == [4, 4, 4]

    # Normalisation: lowercase and without the dashes works too.
    await harness.pair(device, code.replace("-", "").lower())
    assert device.is_paired
    assert device.server_name == "Home Assistant"
    await harness.start_session(device)

    result = await finish_flow(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Wejscie"
    assert result["result"].unique_id == DEVICE_ID
    assert result["data"] == entry_data(device.public_key, ["punches", "relay"])
    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED
    assert hass.states.get(CONNECTION).state == STATE_ON
    await until(lambda: hass.states.get(RELAY).state == STATE_OFF)
    assert device_registry.async_get_device(identifiers={(DOMAIN, DEVICE_ID)})

    # The code was used once.
    other = LinkDevice(device_id="C00F0203")
    with pytest.raises(PairingRejected):
        await harness.pair(other, code)


async def test_pairing_without_session(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """If the device does not open a session soon, the entry is created anyway."""
    result = await start_flow(hass)
    with patch("custom_components.adaraz.config_flow.SESSION_WAIT", 0.05):
        await harness.pair(device, result["description_placeholders"]["code"])
        result = await finish_flow(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CAPS] == []
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTION).state == STATE_OFF
    assert hass.states.get(RELAY) is None

    # The first session brings the capabilities.
    await harness.start_session(device)
    await until(lambda: (state := hass.states.get(RELAY)) and state.state == STATE_OFF)
    assert result["result"].data[CONF_CAPS] == ["punches", "relay"]


async def test_pairing_expired_and_retry(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """An expired code shows an error; submitting starts again with a new code."""
    with patch("custom_components.adaraz.config_flow.PAIRING_TIMEOUT", 0.05):
        result = await start_flow(hass)
        first_code = result["description_placeholders"]["code"]
        await asyncio.sleep(0.1)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["errors"] == {"base": "pairing_expired"}

    # The expired code is rejected by the server.
    with pytest.raises(PairingRejected):
        await harness.pair(device, first_code)

    await retry_and_pair(hass, harness, device, result)


async def test_pairing_too_many_failures(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """Five wrong codes close the window; a new code then works."""
    result = await start_flow(hass)
    code = result["description_placeholders"]["code"]
    wrong = "0000-0000-0000" if code != "0000-0000-0000" else "1111-1111-1111"
    for _ in range(5):
        with pytest.raises(PairingRejected):
            await harness.pair(device, wrong)
    result = await finish_flow(hass, result["flow_id"])
    assert result["errors"] == {"base": "pairing_failed"}
    assert not device.is_paired
    # The closed window does not accept the right code any more.
    with pytest.raises(PairingRejected):
        await harness.pair(device, code)

    await retry_and_pair(hass, harness, device, result)


async def test_pairing_window_cancelled(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """A window cancelled by someone else ends with an error; a retry works."""
    result = await start_flow(hass)
    for window in get_link(hass).server.pairing_windows:
        window.cancel()
    result = await finish_flow(hass, result["flow_id"])
    assert result["errors"] == {"base": "unknown"}

    await retry_and_pair(hass, harness, device, result)


async def test_pairing_storage_error(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """An error while storing the paired device ends with an error; a retry works."""
    result = await start_flow(hass)
    with (
        patch(
            "homeassistant.helpers.storage.Store._async_write_data",
            side_effect=WriteError("disk full"),
        ),
        pytest.raises(PairingRejected),
    ):
        await harness.pair(device, result["description_placeholders"]["code"])
    result = await finish_flow(hass, result["flow_id"])
    assert result["errors"] == {"base": "unknown"}
    assert not device.is_paired

    await retry_and_pair(hass, harness, device, result)


async def test_flow_abort_closes_window(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """Closing the dialog closes the pairing window."""
    result = await start_flow(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    with pytest.raises(PairingRejected):
        await harness.pair(device, result["description_placeholders"]["code"])
    assert not device.is_paired
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_flow_closed_after_pairing(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """A dialog closed after the handshake unpairs the device again."""
    result = await start_flow(hass)
    await harness.pair(device, result["description_placeholders"]["code"])
    await harness.start_session(device)
    await hass.async_block_till_done(wait_background_tasks=True)

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done(wait_background_tasks=True)
    # The connected device is told to delete its pairing.
    assert await harness.session_ended(device) == 1000
    assert not device.is_paired
    assert not hass.config_entries.async_entries(DOMAIN)

    # It pairs again with a new flow.
    result = await start_flow(hass)
    await harness.pair(device, result["description_placeholders"]["code"])
    await harness.start_session(device)
    result = await finish_flow(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_flow_closed_while_waiting_for_session(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """A dialog closed while waiting for the first session forgets the device."""
    result = await start_flow(hass)
    await harness.pair(device, result["description_placeholders"]["code"])
    assert device.is_paired

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not hass.config_entries.async_entries(DOMAIN)
    # The device was offline; its session is refused now.
    await harness.start_session(device, wait=False)
    assert await harness.session_ended(device) == 4001


async def test_already_configured(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """Pairing a configured device again aborts and keeps one entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data=entry_data(device.public_key, ["punches", "relay"]),
    )
    entry.add_to_hass(hass)
    result = await start_flow(hass)
    await harness.pair(device, result["description_placeholders"]["code"])
    await harness.start_session(device)
    result = await finish_flow(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    # The device stays paired.
    assert device.is_paired


async def test_already_configured_new_key(
    hass: HomeAssistant, harness: Harness, device: LinkDevice
) -> None:
    """A device paired again after a factory reset updates the key of its entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=DEVICE_ID, data=entry_data(b"\x11" * 32, ["punches"])
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(RELAY) is None
    result = await start_flow(hass)
    await harness.pair(device, result["description_placeholders"]["code"])
    await harness.start_session(device)
    result = await finish_flow(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == entry_data(device.public_key, ["punches", "relay"])
    await hass.async_block_till_done()
    await until(lambda: (state := hass.states.get(RELAY)) and state.state == STATE_OFF)
    assert hass.states.get(CONNECTION).state == STATE_ON


async def test_repair_without_session_keeps_caps(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    entity_registry: er.EntityRegistry,
) -> None:
    """Re-pairing without a quick session keeps the capabilities and the entities."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="Wejscie",
        data=entry_data(b"\x11" * 32, ["punches", "relay"]),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    relay = entity_registry.async_get(RELAY)
    assert relay is not None
    entity_registry.async_update_entity(RELAY, name="Door")

    result = await start_flow(hass)
    with patch("custom_components.adaraz.config_flow.SESSION_WAIT", 0.05):
        await harness.pair(device, result["description_placeholders"]["code"])
        result = await finish_flow(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert entry.data == entry_data(device.public_key, ["punches", "relay"])
    assert entry.state is ConfigEntryState.LOADED
    kept = entity_registry.async_get(RELAY)
    assert kept is not None
    assert kept.id == relay.id
    assert kept.name == "Door"

    # The device connects under its new key.
    await harness.start_session(device)
    await until(lambda: hass.states.get(RELAY).state == STATE_OFF)


async def test_no_url(hass: HomeAssistant) -> None:
    """Without a URL the reader could use, the flow aborts."""
    with patch(
        "custom_components.adaraz.config_flow.get_url",
        side_effect=NoURLAvailableError,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_url"
