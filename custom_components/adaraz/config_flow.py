"""Config flow for ADARAZ: pair a device with a one-time code."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from adaraz import (
    PairedDevice,
    PairingError,
    PairingExpired,
    PairingFailed,
    PairingWindow,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CONF_CAPS,
    CONF_MODEL,
    CONF_NAME,
    CONF_PUBLIC_KEY,
    DOMAIN,
    SESSION_WAIT,
)
from .link import async_get_link

_LOGGER = logging.getLogger(__name__)

#: Lifetime of a pairing code in seconds (protocol maximum: 10 minutes).
PAIRING_TIMEOUT = 600.0

RESULT_PAIRED = "paired"


class AdarazConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair an ADARAZ device.

    Home Assistant opens a pairing window and shows a one-time code and its URL.
    A person enters both in the device's local admin panel; the device then
    connects and proves that it knows the code (Noise XXpsk3).
    """

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._url = ""
        self._window: PairingWindow | None = None
        self._pair_task: asyncio.Task[str] | None = None
        self._paired: PairedDevice | None = None
        #: Capabilities from the first session; None if it did not come in time.
        self._caps: tuple[str, ...] | None = None
        self._error = ""
        #: The paired device got an entry (created, or an existing one updated).
        self._handed_over = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Open a pairing window and show its code."""
        try:
            self._url = get_url(self.hass, allow_cloud=False, prefer_external=False)
        except NoURLAvailableError:
            return self.async_abort(reason="no_url")
        link = await async_get_link(self.hass)
        if self._window is not None:
            self._window.cancel()
        self._window = link.server.open_pairing(PAIRING_TIMEOUT, import_history=False)
        self._pair_task = None
        return await self.async_step_pair()

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait until the device pairs or the code expires."""
        assert self._window is not None
        if self._pair_task is None:
            self._pair_task = self.hass.async_create_background_task(
                self._async_wait_for_device(self._window), "adaraz pairing"
            )
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pair",
                description_placeholders={
                    "url": self._url,
                    "code": self._window.code,
                    "minutes": str(round(PAIRING_TIMEOUT / 60)),
                },
                progress_task=self._pair_task,
            )
        result = self._pair_task.result()
        self._pair_task = None
        if result == RESULT_PAIRED:
            return self.async_show_progress_done(next_step_id="finish")
        self._error = result
        return self.async_show_progress_done(next_step_id="retry")

    async def _async_wait_for_device(self, window: PairingWindow) -> str:
        """Return ``paired`` or the translation key of the error."""
        try:
            device = await window
        except PairingExpired:
            return "pairing_expired"
        except PairingFailed:
            return "pairing_failed"
        except PairingError:
            return "unknown"
        except asyncio.CancelledError:
            # A claimed window is left alone: a device is being stored right now
            # and ``async_remove`` unpairs it when that is done.
            if window.active:
                window.cancel()
            raise
        except Exception:
            _LOGGER.exception("Unexpected error while pairing")
            return "unknown"
        self._paired = device
        # The device opens its first session right after pairing; its hello says
        # which capabilities (relay) it offers.
        link = await async_get_link(self.hass)
        session = await link.async_wait_for_session(device.public_key, SESSION_WAIT)
        if session is not None:
            self._caps = tuple(session.hello.caps)
        return RESULT_PAIRED

    async def async_step_retry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show why pairing did not complete; submit to get a new code."""
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(
            step_id="retry", data_schema=vol.Schema({}), errors={"base": self._error}
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry (or update the key of an existing one)."""
        device = self._paired
        assert device is not None
        await self.async_set_unique_id(device.device_id, raise_on_progress=False)
        self._handed_over = True
        data: dict[str, Any] = {
            CONF_PUBLIC_KEY: device.public_key.hex(),
            CONF_MODEL: device.model,
            CONF_NAME: device.name,
        }
        if self._caps is not None:
            data[CONF_CAPS] = list(self._caps)
        # A device paired again after a factory reset has a new key. Without a
        # session the capabilities are unknown: keep the entry's (and its entities);
        # the first session updates them.
        self._abort_if_unique_id_configured(updates=data)
        title = device.name or f"{device.model} {device.device_id}".strip()
        return self.async_create_entry(title=title, data={CONF_CAPS: [], **data})

    @callback
    def async_remove(self) -> None:
        """Close the pairing window when the flow is closed.

        If a device paired but the flow ends without handing it over to an entry
        (the dialog was closed), the device is unpaired again: nothing would use
        it, and it would keep a session whose punches are dropped.
        """
        if self._handed_over or (window := self._window) is None:
            return
        if window.active:
            window.cancel()
        self.hass.async_create_background_task(
            self._async_forget(window), "adaraz forget cancelled pairing"
        )

    async def _async_forget(self, window: PairingWindow) -> None:
        """Unpair the device of a closed flow's window, if one paired."""
        try:
            # Done, or a device is being stored right now: then it finishes soon.
            device = await window
        except Exception:
            return  # Nothing paired.
        _LOGGER.debug("Pairing of %s was not completed, unpairing", device.device_id)
        link = await async_get_link(self.hass)
        await link.async_forget(device.public_key, device.device_id)
