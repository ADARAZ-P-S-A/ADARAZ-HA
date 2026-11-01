"""Pairing codes and PSK derivation (protocol section 2, "Pairing code and PSK")."""

from __future__ import annotations

import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .exceptions import InvalidCodeError

__all__ = [
    "ALPHABET",
    "CODE_LENGTH",
    "GROUP_LENGTH",
    "PSK_INFO",
    "PSK_SALT",
    "derive_psk",
    "format_code",
    "generate_code",
    "normalize_code",
]

#: Crockford Base32 alphabet.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: Number of symbols in a pairing code (60 bits).
CODE_LENGTH = 12
#: Symbols per group in the display form ``XXXX-XXXX-XXXX``.
GROUP_LENGTH = 4
PSK_SALT = b"ADARAZ pairing v1"
PSK_INFO = b"psk"

_MAP = str.maketrans({"O": "0", "I": "1", "L": "1"})


def generate_code() -> str:
    """Return a new random pairing code formatted as ``XXXX-XXXX-XXXX``."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return format_code(raw)


def normalize_code(code: str) -> str:
    """Normalise a code as typed by a person.

    Uppercase, remove ``-`` and spaces, map ``O`` to ``0`` and ``I``/``L`` to ``1``.
    Raises :class:`InvalidCodeError` if the result is not 12 alphabet symbols.
    """
    if not code.isascii():
        raise InvalidCodeError("pairing code contains non-ASCII characters")
    norm = code.upper().replace("-", "").replace(" ", "").translate(_MAP)
    if len(norm) != CODE_LENGTH or any(ch not in ALPHABET for ch in norm):
        raise InvalidCodeError(
            f"pairing code must be {CODE_LENGTH} symbols of 0-9 and A-Z (without U)"
        )
    return norm


def format_code(code: str) -> str:
    """Return the display form ``XXXX-XXXX-XXXX`` of a code (normalised first)."""
    norm = normalize_code(code)
    return "-".join(norm[i : i + GROUP_LENGTH] for i in range(0, CODE_LENGTH, GROUP_LENGTH))


def derive_psk(code: str) -> bytes:
    """Derive the 32 byte Noise PSK from a pairing code (normalised first)."""
    norm = normalize_code(code)
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=PSK_SALT, info=PSK_INFO)
    return hkdf.derive(norm.encode("ascii"))
