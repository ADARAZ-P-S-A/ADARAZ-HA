"""Config flow for ADARAZ: pair a device with a one-time code.

The options flow names known cards and turns the raw card UID in the punch
attributes on or off.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from adaraz import (
    PairedDevice,
    PairingError,
    PairingExpired,
    PairingFailed,
    PairingWindow,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .const import (
    CARD_ID_PREFIX,
    CONF_CAPS,
    CONF_CARDS,
    CONF_EXPOSE_UID,
    CONF_MODEL,
    CONF_NAME,
    CONF_PUBLIC_KEY,
    DOMAIN,
    SESSION_WAIT,
)
from .link import async_get_link

if TYPE_CHECKING:
    from .link import AdarazDevice

_LOGGER = logging.getLogger(__name__)

#: Lifetime of a pairing code in seconds (protocol maximum: 10 minutes).
PAIRING_TIMEOUT = 600.0

RESULT_PAIRED = "paired"

CONF_UID = "uid"
#: A card UID as the protocol sends it: 4 to 7 bytes of uppercase hex.
UID_RE = re.compile(r"(?:[0-9A-F]{2}){4,7}")
#: Names in the form of card IDs are reserved: one could equal another card's ID.
CARD_ID_RE = re.compile(rf"{CARD_ID_PREFIX}[0-9a-f]{{8}}", re.IGNORECASE)


def normalize_uid(value: str) -> str | None:
    """Return the UID in protocol form (``04:a1:b2:c3`` -> ``04A1B2C3``) or None."""
    uid = re.sub(r"[\s:-]", "", value).upper()
    return uid if UID_RE.fullmatch(uid) else None


class AdarazConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair an ADARAZ device.

    Home Assistant opens a pairing window and shows a one-time code and its URL.
    A person enters both in the device's local admin panel; the device then
    connects and proves that it knows the code (Noise XXpsk3).
    """

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AdarazOptionsFlow:
        """Return the options flow."""
        return AdarazOptionsFlow()

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


class AdarazOptionsFlow(OptionsFlow):
    """Name known cards; turn the raw card UID in punch attributes on or off.

    Only admins can open an options flow, so it may show card UIDs.
    """

    @property
    def _cards(self) -> dict[str, str]:
        cards: dict[str, str] = self.config_entry.options.get(CONF_CARDS, {})
        return cards

    @property
    def _device(self) -> AdarazDevice | None:
        """Runtime data of the entry; None if it was never set up."""
        device: AdarazDevice | None = getattr(self.config_entry, "runtime_data", None)
        return device

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what to change."""
        menu = ["add_card"]
        if self._cards:
            menu.append("remove_card")
        menu.append("uid")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_add_card(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Give a card a name: one that punched recently, or a UID typed in."""
        errors: dict[str, str] = {}
        device = self._device
        if user_input is not None:
            uid = normalize_uid(user_input[CONF_UID])
            name = user_input[CONF_NAME].strip()
            if uid is None:
                errors[CONF_UID] = "invalid_uid"
            elif not name or CARD_ID_RE.fullmatch(name):
                errors[CONF_NAME] = "invalid_name"
            else:
                if device is not None:
                    device.recent_unnamed.pop(uid, None)
                cards = {**self._cards, uid: name}
                return self._save(**{CONF_CARDS: dict(sorted(cards.items()))})

        uid_selector: SelectSelector | TextSelector = TextSelector()
        if device is not None and (recent := device.recent_unnamed):
            uid_selector = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=uid,
                            label=(
                                f"{device.link.card_id(uid)} ({uid}, {_format_ts(ts)})"
                            ),
                        )
                        for uid, ts in reversed(recent.items())
                    ],
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        return self.async_show_form(
            step_id="add_card",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_UID): uid_selector,
                        vol.Required(CONF_NAME): TextSelector(),
                    }
                ),
                user_input,
            ),
            errors=errors,
        )

    async def async_step_remove_card(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Forget the names of cards; their punches show the card ID again."""
        if user_input is not None:
            remove = set(user_input[CONF_CARDS])
            cards = {
                uid: name for uid, name in self._cards.items() if uid not in remove
            }
            return self._save(**{CONF_CARDS: cards})
        return self.async_show_form(
            step_id="remove_card",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CARDS): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=uid, label=f"{name} ({uid})")
                                for uid, name in self._cards.items()
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_uid(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Turn the raw card UID in the punch attributes on or off."""
        if user_input is not None:
            return self._save(**{CONF_EXPOSE_UID: user_input[CONF_EXPOSE_UID]})
        return self.async_show_form(
            step_id="uid",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EXPOSE_UID,
                        default=bool(self.config_entry.options.get(CONF_EXPOSE_UID)),
                    ): BooleanSelector()
                }
            ),
        )


def _format_ts(ts: int) -> str:
    """Local time of a punch for the list of recent cards."""
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
