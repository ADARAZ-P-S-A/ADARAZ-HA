"""Exceptions raised by the adaraz library."""

from __future__ import annotations

from .const import CLOSE_PROTOCOL_ERROR

__all__ = [
    "AdarazError",
    "ConnectionClosed",
    "DeviceNotConnected",
    "DeviceNotFound",
    "InvalidCodeError",
    "PairingCancelled",
    "PairingError",
    "PairingExpired",
    "PairingFailed",
    "PairingRejected",
    "ProtocolError",
    "RelayTimeout",
]


class AdarazError(Exception):
    """Base class of all adaraz errors."""


class ProtocolError(AdarazError):
    """The peer violated the protocol. ``close_code`` is the WebSocket code to use."""

    def __init__(self, message: str, close_code: int = CLOSE_PROTOCOL_ERROR) -> None:
        super().__init__(message)
        self.close_code = close_code


class ConnectionClosed(AdarazError):
    """The connection is closed."""


class InvalidCodeError(AdarazError, ValueError):
    """A pairing code is malformed (rejected locally, before any network traffic)."""


class PairingError(AdarazError):
    """Base class of pairing window outcomes other than success."""


class PairingExpired(PairingError):
    """The pairing window expired before a device paired."""


class PairingFailed(PairingError):
    """The pairing window was closed after too many failed attempts."""


class PairingCancelled(PairingError):
    """The pairing window was cancelled by the application."""


class PairingRejected(PairingError):
    """Device side: the server rejected the pairing (wrong or expired code)."""

    def __init__(self, message: str, close_code: int | None = None) -> None:
        super().__init__(message)
        self.close_code = close_code


class DeviceNotFound(AdarazError, KeyError):
    """No paired device with the given id."""


class DeviceNotConnected(AdarazError):
    """The device has no active session."""


class RelayTimeout(AdarazError, TimeoutError):
    """The device did not answer a relay command in time."""
