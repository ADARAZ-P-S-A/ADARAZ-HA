"""Minimal Noise Protocol Framework implementation (revision 34).

Only what ADARAZ Link needs is implemented:

* ``Noise_XXpsk3_25519_AESGCM_SHA256`` (pairing)
* ``Noise_IK_25519_AESGCM_SHA256`` (session)

The code follows the structure of the specification (CipherState, SymmetricState,
HandshakeState) so it can be compared with it line by line. All operations are
synchronous and CPU bound only (a few X25519 and AES-GCM operations), so they are safe
to call from an event loop.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "DHLEN",
    "HASHLEN",
    "IK",
    "MAX_MESSAGE_LEN",
    "MAX_NONCE",
    "TAGLEN",
    "XXPSK3",
    "CipherState",
    "DecryptError",
    "HandshakePattern",
    "HandshakeState",
    "KeyPair",
    "NoiseError",
    "SymmetricState",
    "generate_private_key",
    "public_key_from_private",
]

DHLEN = 32
HASHLEN = 32
TAGLEN = 16
PSKLEN = 32
MAX_NONCE = 2**64 - 1
MAX_MESSAGE_LEN = 65535


class NoiseError(Exception):
    """Raised on any Noise level failure (bad message, wrong state, bad key)."""


class DecryptError(NoiseError):
    """Raised when an AEAD tag does not verify."""


# ---------------------------------------------------------------------------
# Crypto functions: 25519, AESGCM, SHA256
# ---------------------------------------------------------------------------


def generate_private_key() -> bytes:
    """Return a new random X25519 private key (32 bytes)."""
    return X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def public_key_from_private(private_key: bytes) -> bytes:
    """Return the X25519 public key (32 bytes) for a 32 byte private key."""
    return KeyPair.from_private(private_key).public


class KeyPair:
    """An X25519 key pair."""

    __slots__ = ("_private", "private", "public")

    def __init__(self, private_key: X25519PrivateKey) -> None:
        self._private = private_key
        self.private = private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        self.public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    @classmethod
    def generate(cls) -> KeyPair:
        return cls(X25519PrivateKey.generate())

    @classmethod
    def from_private(cls, private_key: bytes) -> KeyPair:
        if len(private_key) != DHLEN:
            raise ValueError("X25519 private key must be 32 bytes")
        return cls(X25519PrivateKey.from_private_bytes(private_key))

    def dh(self, public_key: bytes) -> bytes:
        if len(public_key) != DHLEN:
            raise NoiseError("X25519 public key must be 32 bytes")
        try:
            return self._private.exchange(X25519PublicKey.from_public_bytes(public_key))
        except ValueError as err:  # all-zero shared secret (low order point)
            raise NoiseError("invalid X25519 public key") from err

    def __repr__(self) -> str:
        return f"KeyPair(public={self.public.hex()})"


def _hash(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int) -> tuple[bytes, ...]:
    """HKDF as defined in section 4.3 of the Noise specification."""
    temp_key = _hmac(chaining_key, ikm)
    out: list[bytes] = []
    prev = b""
    for i in range(1, outputs + 1):
        prev = _hmac(temp_key, prev + bytes([i]))
        out.append(prev)
    return tuple(out)


def _nonce(n: int) -> bytes:
    # AESGCM: 32 bits of zeros followed by the big-endian 64-bit counter.
    return b"\x00\x00\x00\x00" + n.to_bytes(8, "big")


# ---------------------------------------------------------------------------
# CipherState
# ---------------------------------------------------------------------------


class CipherState:
    """Noise CipherState (section 5.1)."""

    __slots__ = ("_aead", "_k", "n")

    def __init__(self, key: bytes | None = None) -> None:
        self._k: bytes | None = None
        self._aead: AESGCM | None = None
        self.n = 0
        self.initialize_key(key)

    def initialize_key(self, key: bytes | None) -> None:
        self._k = key
        self._aead = AESGCM(key) if key is not None else None
        self.n = 0

    def has_key(self) -> bool:
        return self._k is not None

    def set_nonce(self, nonce: int) -> None:
        self.n = nonce

    def encrypt_with_ad(self, ad: bytes, plaintext: bytes) -> bytes:
        if self._aead is None:
            return plaintext
        if self.n >= MAX_NONCE:
            raise NoiseError("nonce exhausted")
        ciphertext = self._aead.encrypt(_nonce(self.n), plaintext, ad)
        self.n += 1
        return ciphertext

    def decrypt_with_ad(self, ad: bytes, ciphertext: bytes) -> bytes:
        if self._aead is None:
            return ciphertext
        if self.n >= MAX_NONCE:
            raise NoiseError("nonce exhausted")
        if len(ciphertext) < TAGLEN:
            raise DecryptError("ciphertext too short")
        try:
            plaintext = self._aead.decrypt(_nonce(self.n), ciphertext, ad)
        except InvalidTag as err:
            raise DecryptError("authentication failed") from err
        self.n += 1
        return plaintext

    def copy(self) -> CipherState:
        other = CipherState(self._k)
        other.n = self.n
        return other


# ---------------------------------------------------------------------------
# SymmetricState
# ---------------------------------------------------------------------------


class SymmetricState:
    """Noise SymmetricState (section 5.2)."""

    __slots__ = ("cipher", "ck", "h")

    def __init__(self, protocol_name: bytes) -> None:
        if len(protocol_name) <= HASHLEN:
            self.h = protocol_name.ljust(HASHLEN, b"\x00")
        else:
            self.h = _hash(protocol_name)
        self.ck = self.h
        self.cipher = CipherState()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = _hkdf(self.ck, ikm, 2)
        self.cipher.initialize_key(temp_k[:32])

    def mix_hash(self, data: bytes) -> None:
        self.h = _hash(self.h + data)

    def mix_key_and_hash(self, ikm: bytes) -> None:
        self.ck, temp_h, temp_k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.cipher.initialize_key(temp_k[:32])

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.cipher.encrypt_with_ad(self.h, plaintext)
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.cipher.decrypt_with_ad(self.h, ciphertext)
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[CipherState, CipherState]:
        temp_k1, temp_k2 = _hkdf(self.ck, b"", 2)
        return CipherState(temp_k1[:32]), CipherState(temp_k2[:32])

    def copy(self) -> SymmetricState:
        other = object.__new__(SymmetricState)
        other.h = self.h
        other.ck = self.ck
        other.cipher = self.cipher.copy()
        return other


# ---------------------------------------------------------------------------
# Handshake patterns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HandshakePattern:
    """A handshake pattern with its modifiers applied (e.g. ``XXpsk3``)."""

    name: str
    initiator_pre: tuple[str, ...]
    responder_pre: tuple[str, ...]
    messages: tuple[tuple[str, ...], ...]

    @property
    def is_psk(self) -> bool:
        return any("psk" in tokens for tokens in self.messages)

    def protocol_name(self) -> bytes:
        return f"Noise_{self.name}_25519_AESGCM_SHA256".encode("ascii")


XXPSK3 = HandshakePattern(
    name="XXpsk3",
    initiator_pre=(),
    responder_pre=(),
    messages=(("e",), ("e", "ee", "s", "es"), ("s", "se", "psk")),
)

IK = HandshakePattern(
    name="IK",
    initiator_pre=(),
    responder_pre=("s",),
    messages=(("e", "es", "s", "ss"), ("e", "ee", "se")),
)


# ---------------------------------------------------------------------------
# HandshakeState
# ---------------------------------------------------------------------------


class HandshakeState:
    """Noise HandshakeState (section 5.3).

    ``psk`` may be assigned after construction, as long as it is set before the
    ``psk`` token is processed. This lets a responder try several candidate PSKs on a
    copy of the state (see :meth:`copy`).
    """

    def __init__(
        self,
        pattern: HandshakePattern,
        initiator: bool,
        *,
        prologue: bytes = b"",
        s: KeyPair | None = None,
        e: KeyPair | None = None,
        rs: bytes | None = None,
        psk: bytes | None = None,
    ) -> None:
        self.pattern = pattern
        self.initiator = initiator
        self.s = s
        self.e = e
        self.rs = rs
        self.re: bytes | None = None
        self.psk = psk
        self._index = 0
        self._ss = SymmetricState(pattern.protocol_name())
        self._ss.mix_hash(prologue)
        for token in pattern.initiator_pre:
            self._mix_pre(token, local=initiator)
        for token in pattern.responder_pre:
            self._mix_pre(token, local=not initiator)

    def _mix_pre(self, token: str, *, local: bool) -> None:
        if token != "s":  # pre-message "e" is not used by our patterns
            raise NoiseError(f"unsupported pre-message token {token}")
        if local:
            if self.s is None:
                raise NoiseError("local static key required")
            self._ss.mix_hash(self.s.public)
        else:
            if self.rs is None or len(self.rs) != DHLEN:
                raise NoiseError("remote static key required")
            self._ss.mix_hash(self.rs)

    # -- state -------------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self._index >= len(self.pattern.messages)

    @property
    def remote_static(self) -> bytes | None:
        return self.rs

    @property
    def handshake_hash(self) -> bytes:
        return self._ss.h

    def copy(self) -> HandshakeState:
        """Return an independent copy (key pairs are immutable and shared)."""
        other = object.__new__(HandshakeState)
        other.pattern = self.pattern
        other.initiator = self.initiator
        other.s = self.s
        other.e = self.e
        other.rs = self.rs
        other.re = self.re
        other.psk = self.psk
        other._index = self._index
        other._ss = self._ss.copy()
        return other

    def _my_turn(self) -> bool:
        return (self._index % 2 == 0) == self.initiator

    def _dh(self, local: KeyPair | None, remote: bytes | None) -> bytes:
        if local is None or remote is None:
            raise NoiseError("missing key for DH")
        return local.dh(remote)

    def _token_dh(self, token: str) -> None:
        if token == "ee":
            self._ss.mix_key(self._dh(self.e, self.re))
        elif token == "ss":
            self._ss.mix_key(self._dh(self.s, self.rs))
        elif token == "es":
            if self.initiator:
                self._ss.mix_key(self._dh(self.e, self.rs))
            else:
                self._ss.mix_key(self._dh(self.s, self.re))
        elif token == "se":
            if self.initiator:
                self._ss.mix_key(self._dh(self.s, self.re))
            else:
                self._ss.mix_key(self._dh(self.e, self.rs))
        elif token == "psk":
            if self.psk is None or len(self.psk) != PSKLEN:
                raise NoiseError("32 byte psk required")
            self._ss.mix_key_and_hash(self.psk)
        else:
            raise NoiseError(f"unknown token {token}")

    # -- messages ----------------------------------------------------------

    def write_message(self, payload: bytes = b"") -> bytes:
        if self.finished:
            raise NoiseError("handshake already finished")
        if not self._my_turn():
            raise NoiseError("not our turn to write")
        out = bytearray()
        for token in self.pattern.messages[self._index]:
            if token == "e":
                if self.e is None:
                    self.e = KeyPair.generate()
                out += self.e.public
                self._ss.mix_hash(self.e.public)
                if self.pattern.is_psk:
                    self._ss.mix_key(self.e.public)
            elif token == "s":
                if self.s is None:
                    raise NoiseError("local static key required")
                out += self._ss.encrypt_and_hash(self.s.public)
            else:
                self._token_dh(token)
        out += self._ss.encrypt_and_hash(payload)
        if len(out) > MAX_MESSAGE_LEN:
            raise NoiseError("message too large")
        self._index += 1
        return bytes(out)

    def read_message(self, message: bytes) -> bytes:
        """Process a handshake message and return its payload.

        On failure the state must be discarded (it may be partially updated).
        """
        if self.finished:
            raise NoiseError("handshake already finished")
        if self._my_turn():
            raise NoiseError("not our turn to read")
        if len(message) > MAX_MESSAGE_LEN:
            raise NoiseError("message too large")
        pos = 0
        for token in self.pattern.messages[self._index]:
            if token == "e":
                if len(message) - pos < DHLEN:
                    raise NoiseError("message too short")
                self.re = bytes(message[pos : pos + DHLEN])
                pos += DHLEN
                self._ss.mix_hash(self.re)
                if self.pattern.is_psk:
                    self._ss.mix_key(self.re)
            elif token == "s":
                size = DHLEN + (TAGLEN if self._ss.cipher.has_key() else 0)
                if len(message) - pos < size:
                    raise NoiseError("message too short")
                self.rs = self._ss.decrypt_and_hash(bytes(message[pos : pos + size]))
                pos += size
            else:
                self._token_dh(token)
        payload = self._ss.decrypt_and_hash(bytes(message[pos:]))
        self._index += 1
        return payload

    def split(self) -> tuple[CipherState, CipherState]:
        """Return ``(send, receive)`` cipher states for this side."""
        if not self.finished:
            raise NoiseError("handshake not finished")
        c1, c2 = self._ss.split()
        return (c1, c2) if self.initiator else (c2, c1)
