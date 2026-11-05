"""Fixtures for ADARAZ integration tests.

The tests use the Python device implementation from the ``adaraz`` library
(``adaraz.device.LinkDevice``), which talks the real protocol to the integration's
WebSocket endpoint through the aiohttp test client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from adaraz import SUBPROTOCOL
from adaraz.aiohttp_ws import AiohttpTransport
from adaraz.device import LinkDevice
from aiohttp.test_utils import TestClient
import pytest
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.setup import async_setup_component

from custom_components.adaraz.const import DOMAIN, WS_PATH
from custom_components.adaraz.link import DATA_LINK, AdarazLink

DEVICE_ID = "C00F0202"
INTERNAL_URL = "http://ha.local:8123"
PUNCH_ATTRS = ("card", "card_name", "uid", "ts", "late")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable the custom integration in all tests."""


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait until ``condition()`` is true."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def get_link(hass: HomeAssistant) -> AdarazLink:
    """Return the integration's link server."""
    return hass.data[DATA_LINK]


def capture_punches(hass: HomeAssistant, entity_id: str) -> list[dict[str, Any]]:
    """Return a list that collects the punches fired by an event entity.

    A punch is a new state (the time it fired, in milliseconds) with its attributes;
    a state restored when the entity is set up again is not a new punch.
    """
    punches: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def key(state: State) -> tuple[Any, ...]:
        return (state.state, *(state.attributes.get(name) for name in PUNCH_ATTRS))

    if (state := hass.states.get(entity_id)) is not None:
        seen.add(key(state))

    @callback
    def listener(event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None or new.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return
        if (new_key := key(new)) in seen:
            return
        seen.add(new_key)
        punches.append(
            {
                name: new.attributes[name]
                for name in PUNCH_ATTRS
                if name in new.attributes
            }
        )

    async_track_state_change_event(hass, [entity_id], listener)
    return punches


def card_ids(hass: HomeAssistant, *uids: str) -> list[str]:
    """Return the card IDs of these UIDs (the ``card`` attribute of punches)."""
    link = get_link(hass)
    return [link.card_id(uid) for uid in uids]


class Harness:
    """Runs simulated devices against Home Assistant's WebSocket endpoint."""

    def __init__(
        self, hass: HomeAssistant, client_factory: ClientSessionGenerator
    ) -> None:
        """Initialize."""
        self.hass = hass
        self._client_factory = client_factory
        self._client: TestClient | None = None
        self.sessions: dict[LinkDevice, asyncio.Task[int | None]] = {}

    async def connect(self) -> AiohttpTransport:
        """Open a device WebSocket to Home Assistant."""
        if self._client is None:
            self._client = await self._client_factory()
        ws = await self._client.ws_connect(WS_PATH, protocols=(SUBPROTOCOL,))
        return AiohttpTransport(ws)

    async def start_session(self, device: LinkDevice, *, wait: bool = True) -> None:
        """Start a device session in the background (and wait until established)."""
        await self.stop_session(device)
        self.sessions[device] = asyncio.create_task(
            device.run_session(await self.connect())
        )
        if wait:
            await until(
                lambda: get_link(self.hass).server.is_connected(device.device_id)
            )
            await until(lambda: device.welcome is not None and device.connected)

    async def stop_session(self, device: LinkDevice) -> int | None:
        """Stop the device's session and return its close code."""
        task = self.sessions.pop(device, None)
        if task is None:
            return None
        await device.stop()
        code = await task
        await until(
            lambda: not get_link(self.hass).server.is_connected(device.device_id)
        )
        return code

    async def session_ended(self, device: LinkDevice) -> int | None:
        """Wait until the device's session ends by itself; return its close code."""
        task = self.sessions.pop(device)
        async with asyncio.timeout(5):
            return await task

    async def pair(self, device: LinkDevice, code: str) -> None:
        """Pair the device with a code shown by the config flow."""
        await device.pair(await self.connect(), code)

    async def close(self) -> None:
        """Stop all sessions."""
        for device in list(self.sessions):
            await self.stop_session(device)


@pytest.fixture
async def harness(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> AsyncGenerator[Harness]:
    """Return the device harness; Home Assistant has a URL and the domain is set up."""
    await async_process_ha_core_config(hass, {"internal_url": INTERNAL_URL})
    assert await async_setup_component(hass, DOMAIN, {})
    # Components that register HTTP views must be set up before the first test
    # client starts the app; newer aiohttp freezes the router at that point.
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    harness = Harness(hass, hass_client_no_auth)
    yield harness
    await harness.close()


async def start_flow(hass: HomeAssistant) -> dict[str, Any]:
    """Start a user flow, choose RAZ-RCP-1 and return the progress result.

    The progress result has the pairing code.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert result["menu_options"] == ["raz_rcp_1"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "raz_rcp_1"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "pair"
    return result


async def finish_flow(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    """Continue a flow after its pairing task finished."""
    await hass.async_block_till_done(wait_background_tasks=True)
    return await hass.config_entries.flow.async_configure(flow_id)


PairDevice = Callable[[LinkDevice], Awaitable[ConfigEntry]]


@pytest.fixture
def pair_device(hass: HomeAssistant, harness: Harness) -> PairDevice:
    """Return a function that pairs a device through the config flow."""

    async def pair(device: LinkDevice) -> ConfigEntry:
        result = await start_flow(hass)
        await harness.pair(device, result["description_placeholders"]["code"])
        await harness.start_session(device)
        result = await finish_flow(hass, result["flow_id"])
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        entry: ConfigEntry = result["result"]
        return entry

    return pair


@pytest.fixture
def device() -> LinkDevice:
    """Return a simulated reader that allows remote relay control."""
    return LinkDevice(
        device_id=DEVICE_ID, name="Wejscie", relay_allowed=True, open_ms=50
    )
