"""Tests for setting up and unloading ADARAZ."""

from __future__ import annotations

from adaraz import PairedDevice, generate_private_key, public_key_from_private
from adaraz.device import LinkDevice
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .conftest import DEVICE_ID, Harness, PairDevice, get_link
from custom_components.adaraz.const import (
    CONF_CAPS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PUBLIC_KEY,
    DOMAIN,
)
from custom_components.adaraz.link import async_get_link


def mock_entry(public_key: bytes, caps: list[str] | None = None) -> MockConfigEntry:
    """Return an entry for the test device."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        title="Wejscie",
        data={
            CONF_PUBLIC_KEY: public_key.hex(),
            CONF_MODEL: "RAZ-RCP-1",
            CONF_NAME: "Wejscie",
            CONF_CAPS: caps if caps is not None else ["punches", "relay"],
        },
    )


async def test_server_key_generated_once(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """The server key is generated on first start and kept."""
    assert await async_setup_component(hass, DOMAIN, {})
    link = get_link(hass)
    stored = hass_storage[f"{DOMAIN}.server"]["data"]
    assert stored["private_key"] == link.storage.private_key.hex()
    assert link.server.public_key == public_key_from_private(link.storage.private_key)
    assert await async_get_link(hass) is link


async def test_existing_server_key(hass: HomeAssistant, hass_storage: dict) -> None:
    """A stored key is used; devices without an entry are removed at start."""
    private_key = generate_private_key()
    orphan = PairedDevice(public_key=b"\x01" * 32, device_id="DEAD0001")
    kept = PairedDevice(public_key=b"\x02" * 32, device_id=DEVICE_ID)
    hass_storage[f"{DOMAIN}.server"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.server",
        "data": {
            "private_key": private_key.hex(),
            "devices": [orphan.to_dict(), kept.to_dict()],
        },
    }
    mock_entry(kept.public_key).add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    link = get_link(hass)
    assert link.server.public_key == public_key_from_private(private_key)
    assert [d.device_id for d in await link.storage.list_devices()] == [DEVICE_ID]


async def test_setup_unload(hass: HomeAssistant, hass_storage: dict) -> None:
    """An entry sets up offline, restores its device record, and unloads."""
    public_key = b"\x03" * 32
    entry = mock_entry(public_key)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    link = get_link(hass)
    # The device record is restored from the entry (e.g. lost storage).
    assert (await link.storage.get_device(public_key)).device_id == DEVICE_ID
    assert link.devices[public_key] is entry.runtime_data
    assert hass.states.get("binary_sensor.wejscie_connection").state == "off"
    assert hass.states.get("switch.wejscie_relay").state == "unavailable"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert public_key not in link.devices


async def test_setup_with_repaired_key(hass: HomeAssistant) -> None:
    """If the device was paired again under another key, the entry gets that key."""
    assert await async_setup_component(hass, DOMAIN, {})
    link = get_link(hass)
    await link.storage.add_device(
        PairedDevice(public_key=b"\x05" * 32, device_id=DEVICE_ID)
    )
    entry = mock_entry(b"\x04" * 32)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [d.public_key for d in await link.storage.list_devices()] == [b"\x05" * 32]
    assert entry.data[CONF_PUBLIC_KEY] == (b"\x05" * 32).hex()
    assert entry.runtime_data.public_key == b"\x05" * 32


async def test_location_name_and_stop(hass: HomeAssistant, harness: Harness) -> None:
    """The server name follows the location name; stopping closes the server."""
    link = get_link(hass)
    hass.config.location_name = "Biuro"
    hass.bus.async_fire(EVENT_CORE_CONFIG_UPDATE, {})
    await hass.async_block_till_done()
    assert link.server.name == "Biuro"

    window = link.server.open_pairing()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not window.active


async def test_diagnostics(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
    hass_client: ClientSessionGenerator,
) -> None:
    """Diagnostics show the session and redact keys."""
    entry = await pair_device(device)
    device.punch("04000001", ts=1793342337)
    await device.wait_delivered()
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["entry"]["data"][CONF_PUBLIC_KEY] == "**REDACTED**"
    assert diagnostics["device"]["public_key"] == "**REDACTED**"
    assert diagnostics["server"]["server_id"] == "**REDACTED**"
    assert diagnostics["connected"] is True
    assert diagnostics["hello"]["id"] == DEVICE_ID
    assert diagnostics["state"] == {"t": "state", "relay": False, "reader_ok": True}
    assert diagnostics["cursor"] == {"epoch": device.log.epoch, "next": 1}
    assert diagnostics["dedup"] == {
        "seen": 1,
        "newest": 1793342337,
        "replay_ts": None,
    }
    assert device.private_key.hex() not in str(diagnostics)
    assert get_link(hass).storage.private_key.hex() not in str(diagnostics)


async def test_remove_entry_with_repaired_key(hass: HomeAssistant) -> None:
    """Removing an entry also forgets its device paired again under a new key."""
    assert await async_setup_component(hass, DOMAIN, {})
    link = get_link(hass)
    await link.storage.add_device(
        PairedDevice(public_key=b"\x05" * 32, device_id=DEVICE_ID)
    )
    entry = mock_entry(b"\x04" * 32)
    entry.add_to_hass(hass)
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await link.storage.list_devices() == []
