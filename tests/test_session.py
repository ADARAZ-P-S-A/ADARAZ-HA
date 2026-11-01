"""End to end: a simulated reader paired through the flow, over the real endpoint."""

from __future__ import annotations

import time
from unittest.mock import patch

from adaraz import DeviceSession, RelayResult, RelayTimeout, State
from adaraz.device import LinkDevice
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from .conftest import DEVICE_ID, Harness, PairDevice, capture_punches, get_link, until
from custom_components.adaraz.const import DOMAIN
from custom_components.adaraz.storage import AdarazStorage, key_fingerprint

EVENT_ENTITY = "event.wejscie_punch"
CONNECTION = "binary_sensor.wejscie_connection"
READER = "binary_sensor.wejscie_card_reader"
OPEN = "button.wejscie_open"
RELAY = "switch.wejscie_relay"


def device_store(hass_storage: dict, device: LinkDevice) -> dict:
    """Return the stored delivery state of a device."""
    return hass_storage[f"{DOMAIN}.device.{key_fingerprint(device.public_key)}"]["data"]


async def test_entities_and_device(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The device and all entities are created; state follows the device."""
    device.fw = "107"
    entry = await pair_device(device)
    assert entry.state is ConfigEntryState.LOADED

    registry_device = device_registry.async_get_device(
        identifiers={(DOMAIN, DEVICE_ID)}
    )
    assert registry_device is not None
    assert registry_device.manufacturer == "ADARAZ"
    assert registry_device.model == "RAZ-RCP-1"
    assert registry_device.sw_version == "107"
    assert registry_device.serial_number == DEVICE_ID
    assert registry_device.name == "Wejscie"

    for entity_id, unique_id in (
        (EVENT_ENTITY, f"{DEVICE_ID}_punch"),
        (CONNECTION, f"{DEVICE_ID}_connection"),
        (READER, f"{DEVICE_ID}_reader"),
        (OPEN, f"{DEVICE_ID}_open"),
        (RELAY, f"{DEVICE_ID}_relay"),
    ):
        assert entity_registry.async_get(entity_id).unique_id == unique_id

    await until(lambda: hass.states.get(READER).state == STATE_OFF)
    assert hass.states.get(CONNECTION).state == STATE_ON
    assert hass.states.get(RELAY).state == STATE_OFF
    assert hass.states.get(OPEN).state == STATE_UNKNOWN
    assert hass.states.get(EVENT_ENTITY).state == STATE_UNKNOWN

    await device.set_state(reader_ok=False)
    await until(lambda: hass.states.get(READER).state == STATE_ON)

    # Disconnected: relay entities and the reader state are unavailable.
    await harness.stop_session(device)
    await hass.async_block_till_done()
    assert hass.states.get(CONNECTION).state == STATE_OFF
    assert hass.states.get(READER).state == STATE_UNAVAILABLE
    assert hass.states.get(OPEN).state == STATE_UNAVAILABLE
    assert hass.states.get(RELAY).state == STATE_UNAVAILABLE
    assert hass.states.get(EVENT_ENTITY).state == STATE_UNKNOWN

    # Reconnected with a new firmware version.
    device.fw = "108"
    await harness.start_session(device)
    await until(lambda: hass.states.get(READER).state == STATE_ON)
    assert hass.states.get(CONNECTION).state == STATE_ON
    assert hass.states.get(RELAY).state == STATE_OFF
    registry_device = device_registry.async_get_device(
        identifiers={(DOMAIN, DEVICE_ID)}
    )
    assert registry_device.sw_version == "108"


async def test_punches(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
    hass_storage: dict,
) -> None:
    """Punches fire the event entity, late ones flagged."""
    old = int(time.time()) - 3600
    device.punch("04A1B2C3", ts=old)  # before pairing: not imported
    entry = await pair_device(device)
    events = capture_punches(hass, EVENT_ENTITY)

    now = int(time.time())
    device.punch("04FFEE11", ts=now)
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert events == [{"uid": "04FFEE11", "ts": now, "late": False}]
    state = hass.states.get(EVENT_ENTITY)
    assert state.attributes["event_type"] == "punch"
    assert state.attributes["uid"] == "04FFEE11"
    assert state.attributes["ts"] == now
    assert state.attributes["late"] is False

    # A punch from 10 minutes ago (the device was offline) is late.
    device.punch("04A1B2C3D4E5F6", ts=now - 600)
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert events[-1] == {"uid": "04A1B2C3D4E5F6", "ts": now - 600, "late": True}
    state = hass.states.get(EVENT_ENTITY)
    assert state.attributes["late"] is True
    assert state.attributes["uid"] == "04A1B2C3D4E5F6"
    assert len(events) == 2

    # The cursor was stored before the ack.
    assert device_store(hass_storage, device)["cursor"] == {
        "epoch": device.log.epoch,
        "next": device.log.count,
    }
    assert entry.runtime_data.connected


async def test_cursor_across_reconnect(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
    hass_storage: dict,
) -> None:
    """After a reconnect only new punches are delivered, without duplicates."""
    await pair_device(device)
    events = capture_punches(hass, EVENT_ENTITY)
    now = int(time.time())
    device.punch("04000001", ts=now - 40)
    device.punch("04000002", ts=now - 30)
    await device.wait_delivered()
    await harness.stop_session(device)
    cursor = device_store(hass_storage, device)["cursor"]
    assert cursor == {"epoch": device.log.epoch, "next": 2}

    # Offline punches, then the device reconnects.
    device.punch("04000003", ts=now - 20)
    device.punch("04000004", ts=now - 10)
    await harness.start_session(device)
    assert device.welcome.cursor.next == 2
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert [e["uid"] for e in events] == [
        "04000001",
        "04000002",
        "04000003",
        "04000004",
    ]
    assert not any(e["late"] for e in events)

    # A fresh storage instance (as after a restart) reads the same cursor.
    storage = AdarazStorage(hass)
    await storage.async_load()
    stored = await storage.load_cursor(device.public_key)
    assert (stored.epoch, stored.next) == (device.log.epoch, 4)
    assert storage.private_key == get_link(hass).storage.private_key

    # The log is rewritten (a record deleted): the device sends everything again
    # from index 0 under a new epoch; nothing is reported twice, the new punch is.
    await harness.stop_session(device)
    device.log.delete(0)
    device.punch("04000005")
    await harness.start_session(device)
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert [e["uid"] for e in events][4:] == ["04000005"]
    stored = device_store(hass_storage, device)
    assert stored["cursor"] == {"epoch": device.log.epoch, "next": device.log.count}
    # History up to the newest punch before the change is not reported again.
    assert stored["replay_ts"] == now - 10


async def test_punches_refused_while_unloaded(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """While the entry is unloaded the server does not ack, so no punch is lost."""
    entry = await pair_device(device)
    events = capture_punches(hass, EVENT_ENTITY)
    assert await hass.config_entries.async_unload(entry.entry_id)
    device.punch("04000001")
    assert await harness.session_ended(device) == 1011
    assert not events
    assert not device.delivered()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await harness.start_session(device)
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert [e["uid"] for e in events] == ["04000001"]


async def test_relay(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """The button and the switch send relay commands; state comes from the device."""
    await pair_device(device)
    await until(lambda: hass.states.get(RELAY).state == STATE_OFF)

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: RELAY}, blocking=True
    )
    assert device.relay_commands[-1].cmd == "on"
    assert device.relay is True
    assert hass.states.get(RELAY).state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: RELAY}, blocking=True
    )
    assert device.relay_commands[-1].cmd == "off"
    assert hass.states.get(RELAY).state == STATE_OFF

    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: OPEN}, blocking=True
    )
    assert device.relay_commands[-1].cmd == "open"
    assert hass.states.get(RELAY).state == STATE_ON
    # The pulse ends on the device, which reports its state.
    await until(lambda: hass.states.get(RELAY).state == STATE_OFF)

    # A second open within one second: the device answers busy.
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: OPEN}, blocking=True
        )
    assert err.value.translation_key == "relay_busy"
    assert str(err.value) == "The relay is busy. Try again in a moment"
    await until(lambda: device._pulse_task is None)


@pytest.mark.parametrize(
    ("relay_allowed", "result", "translation_key"),
    [
        (False, None, "relay_not_allowed"),
        (True, "timeout", "relay_timeout"),
        (True, "weird", "relay_failed"),
        (True, "invalid", "relay_invalid"),
    ],
)
async def test_relay_errors(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
    relay_allowed: bool,
    result: str | None,
    translation_key: str,
) -> None:
    """Relay failures are reported as translated errors."""
    entry = await pair_device(device)
    runtime = entry.runtime_data
    if not relay_allowed:
        # The capability was withdrawn on the device during the session.
        device.relay_allowed = False
        with pytest.raises(HomeAssistantError) as err:
            await runtime.async_relay("open")
    else:
        effect = RelayTimeout("t") if result == "timeout" else None
        value = RelayResult(id=1, ok=False, err=result)
        with (
            patch.object(
                DeviceSession, "relay", side_effect=effect, return_value=value
            ),
            pytest.raises(HomeAssistantError) as err,
        ):
            await runtime.async_relay("on")
    assert err.value.translation_key == translation_key


async def test_relay_not_connected(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """A relay command without a session fails with a translated error."""
    entry = await pair_device(device)
    await harness.stop_session(device)
    with pytest.raises(HomeAssistantError) as err:
        await entry.runtime_data.async_relay("on")
    assert err.value.translation_key == "not_connected"


async def test_no_relay_capability(
    hass: HomeAssistant,
    harness: Harness,
    pair_device: PairDevice,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without the relay capability there is no button and no switch."""
    device = LinkDevice(
        device_id=DEVICE_ID, name="Wejscie", relay_allowed=False, open_ms=50
    )
    entry = await pair_device(device)
    assert hass.states.get(OPEN) is None
    assert hass.states.get(RELAY) is None
    assert hass.states.get(EVENT_ENTITY) is not None

    # Remote relay control enabled on the device: the entry reloads with the entities.
    await harness.stop_session(device)
    device.relay_allowed = True
    await harness.start_session(device)
    await hass.async_block_till_done()
    await until(
        lambda: entry.state is ConfigEntryState.LOADED and hass.states.get(OPEN)
    )
    assert entry.data["caps"] == ["punches", "relay"]
    await until(lambda: hass.states.get(RELAY).state == STATE_OFF)

    # And disabled again: the entities are removed.
    await harness.stop_session(device)
    device.relay_allowed = False
    await harness.start_session(device)
    await hass.async_block_till_done()
    await until(
        lambda: entry.state is ConfigEntryState.LOADED and not hass.states.get(OPEN)
    )
    assert entity_registry.async_get(OPEN) is None
    assert entity_registry.async_get(RELAY) is None
    assert (
        hass.states.get(BINARY_SENSOR_DOMAIN + ".wejscie_connection").state == STATE_ON
    )


async def test_unpair_on_removal(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
    hass_storage: dict,
) -> None:
    """Deleting the entry tells the device to unpair and forgets it."""
    entry = await pair_device(device)
    device.punch("04000001")
    await device.wait_delivered()
    assert f"{DOMAIN}.device.{key_fingerprint(device.public_key)}" in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await harness.session_ended(device) == 1000
    assert not device.is_paired
    link = get_link(hass)
    assert await link.storage.list_devices() == []
    assert f"{DOMAIN}.device.{key_fingerprint(device.public_key)}" not in hass_storage
    assert hass_storage[f"{DOMAIN}.server"]["data"]["devices"] == []

    # The forgotten device is unknown now.
    device.server_public_key = link.server.public_key
    await harness.start_session(device, wait=False)
    assert await harness.session_ended(device) == 4001


async def test_unpair_offline(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """Deleting the entry of a disconnected device forgets it."""
    entry = await pair_device(device)
    await harness.stop_session(device)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await get_link(hass).storage.list_devices() == []
    # The device still thinks it is paired and is refused.
    await harness.start_session(device, wait=False)
    assert await harness.session_ended(device) == 4001


async def test_disabled_entry_drops_punches(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """Punches of a disabled entry are acknowledged but not reported."""
    entry = await pair_device(device)
    events = capture_punches(hass, EVENT_ENTITY)
    await hass.config_entries.async_set_disabled_by(
        entry.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    device.punch("04000001")
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert not events


async def test_remove_entry_of_unknown_device(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """An entry whose device is no longer paired is removed without errors."""
    entry = await pair_device(device)
    await harness.stop_session(device)
    link = get_link(hass)
    await link.storage.remove_device(device.public_key)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await link.storage.list_devices() == []


async def test_state_without_session(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """A relay result updates the switch even before a state message."""
    entry = await pair_device(device)
    runtime = entry.runtime_data
    runtime.state = None
    with patch.object(
        DeviceSession, "relay", return_value=RelayResult(id=1, ok=True, relay=True)
    ):
        await runtime.async_relay("on")
    assert runtime.state == State(relay=True, reader_ok=True)


async def test_two_devices_routed_by_key(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """Punches, state and relay commands go by key; a device cannot take an id."""
    other = LinkDevice(
        device_id="C00F0203", name="Wyjscie", relay_allowed=True, open_ms=50
    )
    await pair_device(device)
    await pair_device(other)
    events = capture_punches(hass, EVENT_ENTITY)
    other_events = capture_punches(hass, "event.wyjscie_punch")

    other.punch("04000002")
    await other.wait_delivered()
    device.punch("04000001")
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert [e["uid"] for e in events] == ["04000001"]
    assert [e["uid"] for e in other_events] == ["04000002"]

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.wyjscie_relay"},
        blocking=True,
    )
    assert [c.cmd for c in other.relay_commands] == ["on"]
    assert device.relay_commands == []
    assert hass.states.get("switch.wyjscie_relay").state == STATE_ON
    assert hass.states.get(RELAY).state == STATE_OFF

    # The first device reports the second one's id: refused, nothing changes.
    await harness.stop_session(device)
    device.device_id = "C00F0203"
    await harness.start_session(device, wait=False)
    assert await harness.session_ended(device) == 4001
    assert hass.states.get("binary_sensor.wyjscie_connection").state == STATE_ON
    assert hass.states.get(CONNECTION).state == STATE_OFF
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: "button.wyjscie_open"},
        blocking=True,
    )
    assert [c.cmd for c in other.relay_commands] == ["on", "open"]
    assert [e["uid"] for e in other_events] == ["04000002"]


async def test_punches_refused_while_stopping(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """While Home Assistant stops, batches are not acknowledged."""
    await pair_device(device)
    events = capture_punches(hass, EVENT_ENTITY)
    hass.set_state(CoreState.stopping)
    device.punch("04000001")
    assert await harness.session_ended(device) == 1011
    assert not device.delivered()
    assert not events

    hass.set_state(CoreState.running)
    await harness.start_session(device)
    await device.wait_delivered()
    await hass.async_block_till_done()
    assert [e["uid"] for e in events] == ["04000001"]


async def test_readme_automation(
    hass: HomeAssistant,
    harness: Harness,
    device: LinkDevice,
    pair_device: PairDevice,
) -> None:
    """The automation from the README runs for live punches of the card only."""
    entry = await pair_device(device)
    calls = async_mock_service(hass, "test", "open")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "triggers": {
                    "trigger": "state",
                    "entity_id": EVENT_ENTITY,
                    "not_from": "unavailable",
                    "not_to": ["unavailable", "unknown"],
                },
                "conditions": {
                    "condition": "template",
                    "value_template": (
                        "{{ trigger.to_state.attributes.uid == '04A1B2C3'"
                        " and not trigger.to_state.attributes.late }}"
                    ),
                },
                "actions": {"action": "test.open"},
            }
        },
    )
    now = int(time.time())

    async def punch(uid: str, ts: int) -> None:
        device.punch(uid, ts=ts)
        await until(lambda: hass.states.get(EVENT_ENTITY).attributes.get("ts") == ts)
        await hass.async_block_till_done()

    await punch("04A1B2C3", now)
    assert len(calls) == 1
    await punch("04FFFFFF", now + 1)
    await punch("04A1B2C3", now - 600)  # late
    assert len(calls) == 1
    await punch("04A1B2C3", now + 2)
    assert len(calls) == 2

    # Reloading the entry restores the last state; that is not a punch.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(EVENT_ENTITY).attributes["ts"] == now + 2
    assert len(calls) == 2
