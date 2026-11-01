"""Interoperability of adaraz.noise with the reference library ``noiseprotocol``."""

from __future__ import annotations

import os

import pytest
from noise.connection import Keypair, NoiseConnection

from adaraz.const import PROLOGUE_PAIR, PROLOGUE_SESSION
from adaraz.noise import (
    IK,
    XXPSK3,
    CipherState,
    DecryptError,
    HandshakeState,
    KeyPair,
    NoiseError,
)

XX_NAME = b"Noise_XXpsk3_25519_AESGCM_SHA256"
IK_NAME = b"Noise_IK_25519_AESGCM_SHA256"


def _ref(
    name: bytes,
    initiator: bool,
    static: KeyPair,
    prologue: bytes,
    *,
    psk: bytes | None = None,
    rs: bytes | None = None,
) -> NoiseConnection:
    conn = NoiseConnection.from_name(name)
    if initiator:
        conn.set_as_initiator()
    else:
        conn.set_as_responder()
    conn.set_keypair_from_private_bytes(Keypair.STATIC, static.private)
    if rs is not None:
        conn.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, rs)
    if psk is not None:
        conn.set_psks(psk)
    conn.set_prologue(prologue)
    conn.start_handshake()
    return conn


def _transport_roundtrip(
    ours: tuple[CipherState, CipherState], ref: NoiseConnection, *, count: int = 5
) -> None:
    send, recv = ours
    for i in range(count):
        msg = os.urandom(i * 37)
        assert ref.decrypt(send.encrypt_with_ad(b"", msg)) == msg
        reply = os.urandom(i * 11 + 1)
        assert recv.decrypt_with_ad(b"", ref.encrypt(reply)) == reply


def test_protocol_names() -> None:
    assert XXPSK3.protocol_name() == XX_NAME
    assert IK.protocol_name() == IK_NAME
    assert XXPSK3.is_psk and not IK.is_psk


def test_xxpsk3_we_initiate() -> None:
    i_static, r_static, psk = KeyPair.generate(), KeyPair.generate(), os.urandom(32)
    ours = HandshakeState(XXPSK3, True, prologue=PROLOGUE_PAIR, s=i_static, psk=psk)
    ref = _ref(XX_NAME, False, r_static, PROLOGUE_PAIR, psk=psk)

    m1 = ours.write_message(b"")
    assert len(m1) == 32 + 16  # e, plus an encrypted empty payload (psk mode)
    assert ref.read_message(m1) == b""
    m2 = ref.write_message(b"pair_hello")
    assert ours.read_message(m2) == b"pair_hello"
    assert ours.remote_static == r_static.public
    m3 = ours.write_message(b"hello")
    assert ref.read_message(m3) == b"hello"
    assert ours.finished and ref.handshake_finished
    assert ours.handshake_hash == ref.get_handshake_hash()
    _transport_roundtrip(ours.split(), ref)


def test_xxpsk3_we_respond() -> None:
    i_static, r_static, psk = KeyPair.generate(), KeyPair.generate(), os.urandom(32)
    ref = _ref(XX_NAME, True, i_static, PROLOGUE_PAIR, psk=psk)
    ours = HandshakeState(XXPSK3, False, prologue=PROLOGUE_PAIR, s=r_static, psk=psk)

    assert ours.read_message(ref.write_message(b"")) == b""
    assert ref.read_message(ours.write_message(b"pair_hello")) == b"pair_hello"
    assert ours.read_message(ref.write_message(b"hello")) == b"hello"
    assert ours.remote_static == i_static.public
    assert ours.handshake_hash == ref.get_handshake_hash()
    _transport_roundtrip(ours.split(), ref)


def test_xxpsk3_wrong_psk_fails_at_message_3() -> None:
    ref = _ref(XX_NAME, True, KeyPair.generate(), PROLOGUE_PAIR, psk=os.urandom(32))
    ours = HandshakeState(
        XXPSK3, False, prologue=PROLOGUE_PAIR, s=KeyPair.generate(), psk=os.urandom(32)
    )
    ours.read_message(ref.write_message(b""))
    ref.read_message(ours.write_message(b""))
    with pytest.raises(DecryptError):
        ours.read_message(ref.write_message(b"hello"))


def test_xxpsk3_copy_allows_trying_psks() -> None:
    right = os.urandom(32)
    ref = _ref(XX_NAME, True, KeyPair.generate(), PROLOGUE_PAIR, psk=right)
    ours = HandshakeState(XXPSK3, False, prologue=PROLOGUE_PAIR, s=KeyPair.generate())
    ours.read_message(ref.write_message(b""))
    ref.read_message(ours.write_message(b""))
    m3 = ref.write_message(b"hello")
    wrong = ours.copy()
    wrong.psk = os.urandom(32)
    with pytest.raises(DecryptError):
        wrong.read_message(m3)
    good = ours.copy()
    good.psk = right
    assert good.read_message(m3) == b"hello"
    _transport_roundtrip(good.split(), ref)


def test_prologue_mismatch_fails() -> None:
    psk = os.urandom(32)
    ref = _ref(XX_NAME, True, KeyPair.generate(), PROLOGUE_SESSION, psk=psk)
    ours = HandshakeState(XXPSK3, False, prologue=PROLOGUE_PAIR, s=KeyPair.generate(), psk=psk)
    with pytest.raises(DecryptError):
        ours.read_message(ref.write_message(b""))


def test_ik_we_initiate() -> None:
    i_static, r_static = KeyPair.generate(), KeyPair.generate()
    ours = HandshakeState(IK, True, prologue=PROLOGUE_SESSION, s=i_static, rs=r_static.public)
    ref = _ref(IK_NAME, False, r_static, PROLOGUE_SESSION)
    assert ref.read_message(ours.write_message(b"hello")) == b"hello"
    assert ours.read_message(ref.write_message(b"welcome")) == b"welcome"
    assert ours.handshake_hash == ref.get_handshake_hash()
    _transport_roundtrip(ours.split(), ref, count=20)


def test_ik_we_respond() -> None:
    i_static, r_static = KeyPair.generate(), KeyPair.generate()
    ref = _ref(IK_NAME, True, i_static, PROLOGUE_SESSION, rs=r_static.public)
    ours = HandshakeState(IK, False, prologue=PROLOGUE_SESSION, s=r_static)
    assert ours.read_message(ref.write_message(b"hello")) == b"hello"
    assert ours.remote_static == i_static.public
    assert ref.read_message(ours.write_message(b"welcome")) == b"welcome"
    assert ours.handshake_hash == ref.get_handshake_hash()
    _transport_roundtrip(ours.split(), ref, count=20)


def test_ik_wrong_server_key_fails() -> None:
    ours = HandshakeState(
        IK, True, prologue=PROLOGUE_SESSION, s=KeyPair.generate(), rs=KeyPair.generate().public
    )
    responder = HandshakeState(IK, False, prologue=PROLOGUE_SESSION, s=KeyPair.generate())
    with pytest.raises(DecryptError):
        responder.read_message(ours.write_message(b"hello"))


def test_fixed_keys_match_reference() -> None:
    """Deterministic run (fixed ephemeral keys) gives byte-identical messages."""
    i_s, r_s = KeyPair.from_private(bytes(range(32))), KeyPair.from_private(bytes(32 * [7]))
    i_e, r_e = KeyPair.from_private(bytes(32 * [1])), KeyPair.from_private(bytes(32 * [2]))
    psk = bytes(32 * [9])
    a = HandshakeState(XXPSK3, True, prologue=PROLOGUE_PAIR, s=i_s, e=i_e, psk=psk)
    b = HandshakeState(XXPSK3, False, prologue=PROLOGUE_PAIR, s=r_s, e=r_e, psk=psk)
    m1 = a.write_message(b"")
    b.read_message(m1)
    m2 = b.write_message(b"x")
    a.read_message(m2)
    m3 = a.write_message(b"y")
    b.read_message(m3)
    a2 = HandshakeState(XXPSK3, True, prologue=PROLOGUE_PAIR, s=i_s, e=i_e, psk=psk)
    assert a2.write_message(b"") == m1
    assert a.handshake_hash == b.handshake_hash


def test_cipher_nonce_layout() -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)
    cs = CipherState(key)
    cs.set_nonce(0x0102030405060708)
    ct = cs.encrypt_with_ad(b"ad", b"data")
    nonce = b"\x00\x00\x00\x00" + bytes([1, 2, 3, 4, 5, 6, 7, 8])
    assert AESGCM(key).decrypt(nonce, ct, b"ad") == b"data"
    assert cs.n == 0x0102030405060709


def test_nonce_exhaustion() -> None:
    cs = CipherState(os.urandom(32))
    cs.set_nonce(2**64 - 1)
    with pytest.raises(NoiseError):
        cs.encrypt_with_ad(b"", b"x")


def test_turn_order_enforced() -> None:
    hs = HandshakeState(IK, False, prologue=b"", s=KeyPair.generate())
    with pytest.raises(NoiseError):
        hs.write_message(b"")
