from __future__ import annotations

import re

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from adaraz import InvalidCodeError, derive_psk, format_code, generate_code, normalize_code
from adaraz.codes import ALPHABET, CODE_LENGTH


def test_generate_format() -> None:
    codes = {generate_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){2}", code)
        assert normalize_code(code) == code.replace("-", "")


def test_code_length() -> None:
    assert CODE_LENGTH == 12
    assert len(normalize_code(generate_code())) == 12


def test_alphabet() -> None:
    assert len(ALPHABET) == 32
    assert not set("ILOU") & set(ALPHABET)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ABCD-EFGH-1234", "ABCDEFGH1234"),
        ("abcd efgh 1234", "ABCDEFGH1234"),
        ("abcdefgh1234", "ABCDEFGH1234"),
        (" o0ii-lLLO-OZ00 ", "001111100Z00"),
        ("A-B-C-D-E-F-G-H-J-K-M-N", "ABCDEFGHJKMN"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ABCDE-FGH12",  # old 10 symbol format
        "ABCD-EFGH-123",
        "ABCD-EFGH-12345",
        "ABCD-EFGH-12U4",
        "ABCD_EFGH_1234",
        "ĄBCD-EFGH-1234",
    ],
)
def test_normalize_rejects(raw: str) -> None:
    with pytest.raises(InvalidCodeError):
        normalize_code(raw)


def test_format() -> None:
    assert format_code("abcdefgh1o2l") == "ABCD-EFGH-1021"
    assert format_code("ABCD-EFGH-1234") == "ABCD-EFGH-1234"
    with pytest.raises(InvalidCodeError):
        format_code("ABCDEFGH12")


def test_psk_derivation() -> None:
    expected = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"ADARAZ pairing v1", info=b"psk"
    ).derive(b"ABCDEFGH1234")
    assert derive_psk("abcd-efgh-1234") == expected
    assert derive_psk("ABCDEFGH1234") == expected


def test_psk_vector() -> None:
    """Fixed test vector for other implementations (firmware): code ``0123-4567-89AB``."""
    expected = "377690b4f263fcbe5c12a80d283e43febcb90b05326ca85477c0861940cf99a4"
    assert derive_psk("0123-4567-89AB").hex() == expected
    assert derive_psk("0123456789ab").hex() == expected
