from __future__ import annotations

import json

import pytest

from adaraz import Cursor, ProtocolError
from adaraz.messages import (
    Ack,
    Hello,
    LogInfo,
    Message,
    Paired,
    PairHello,
    Ping,
    Pong,
    Punch,
    Punches,
    Relay,
    RelayResult,
    State,
    UnknownMessage,
    Unpair,
    Welcome,
    decode,
    encode,
)

# Examples from docs/protocol.md, section 3 and 5.
SPEC_EXAMPLES = [
    '{"t": "pair_hello", "proto": 1, "server": "Home Assistant", "name": "Dom"}',
    '{"t": "paired", "server_id": "c5e1c5e1c5e1c5e1c5e1c5e1c5e1c5e1", "name": "Dom"}',
    '{"t": "hello", "proto": 1, "model": "RAZ-RCP-1", "fw": "107", "id": "C00F0202",'
    ' "name": "Wejscie glowne", "caps": ["punches", "relay"],'
    ' "log": {"epoch": "9f2c1a7b", "count": 1234}}',
    '{"t": "punches", "epoch": "9f2c1a7b", "from": 1200,'
    ' "items": [[1200, "04A1B2C3", 1793342337], [1202, "04FFEE11", 1793342400]],'
    ' "next": 1203}',
    '{"t": "state", "relay": false, "reader_ok": true}',
    '{"t": "relay_result", "id": 7, "ok": true, "relay": true}',
    '{"t": "relay_result", "id": 8, "ok": false, "err": "not_allowed"}',
    '{"t": "ping", "id": 3}',
    '{"t": "pong", "id": 3}',
    '{"t": "welcome", "proto": 1, "server": "Home Assistant", "name": "Dom",'
    ' "cursor": {"epoch": "9f2c1a7b", "next": 1200}}',
    '{"t": "welcome", "proto": 1, "server": "Home Assistant", "name": "Dom", "cursor": null}',
    '{"t": "ack", "epoch": "9f2c1a7b", "next": 1203}',
    '{"t": "relay", "id": 7, "cmd": "open"}',
    '{"t": "relay", "id": 8, "cmd": "pulse", "ms": 1500}',
    '{"t": "unpair"}',
]


@pytest.mark.parametrize("text", SPEC_EXAMPLES)
def test_spec_examples_roundtrip(text: str) -> None:
    message = decode(text.encode())
    assert not isinstance(message, UnknownMessage)
    assert json.loads(encode(message)) == json.loads(text)
    assert decode(encode(message)) == message


def test_decoded_values() -> None:
    hello = decode(SPEC_EXAMPLES[2].encode())
    assert hello == Hello(
        id="C00F0202",
        model="RAZ-RCP-1",
        fw="107",
        name="Wejscie glowne",
        caps=("punches", "relay"),
        log=LogInfo("9f2c1a7b", 1234),
    )
    punches = decode(SPEC_EXAMPLES[3].encode())
    assert isinstance(punches, Punches)
    assert punches.from_ == 1200 and punches.next == 1203
    assert punches.items == (
        Punch(1200, "04A1B2C3", 1793342337),
        Punch(1202, "04FFEE11", 1793342400),
    )
    welcome = decode(SPEC_EXAMPLES[9].encode())
    assert isinstance(welcome, Welcome) and welcome.cursor == Cursor("9f2c1a7b", 1200)


def test_unknown_fields_and_types() -> None:
    assert decode(b'{"t":"state","relay":true,"reader_ok":false,"new":1}') == State(
        relay=True, reader_ok=False
    )
    unknown = decode(b'{"t":"future","x":[1]}')
    assert isinstance(unknown, UnknownMessage) and unknown.t == "future"
    assert unknown.data["x"] == [1]


def test_encode_is_compact_utf8() -> None:
    data = encode(Hello(id="X", name="Wejście", log=LogInfo("00000000", 0)))
    assert b" " not in data.replace("Wejście".encode(), b"")
    assert "Wejście".encode() in data


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe",
        b"not json",
        b"[1,2]",
        b'{"x":1}',
        b'{"t":5}',
        b'{"t":"state","relay":1,"reader_ok":true}',
        b'{"t":"state","relay":true}',
        b'{"t":"ack","epoch":"9F2C1A7B","next":1}',
        b'{"t":"ack","epoch":"9f2c1a7","next":1}',
        b'{"t":"ack","epoch":"9f2c1a7b","next":-1}',
        b'{"t":"ack","epoch":"9f2c1a7b","next":1.0}',
        b'{"t":"ack","epoch":"9f2c1a7b","next":true}',
        b'{"t":"ping","id":NaN}',
        b'{"t":"hello","proto":1,"id":"","log":{"epoch":"00000000","count":0}}',
        b'{"t":"hello","proto":1,"id":"A"}',
        b'{"t":"paired","server_id":"ABC","name":""}',
        b'{"t":"welcome","proto":1}',
        b'{"t":"relay","id":"7","cmd":"open"}',
        b'{"t":"punches","epoch":"00000000","from":0,"items":[[0,"04a1b2c3",1]],"next":1}',
        b'{"t":"punches","epoch":"00000000","from":0,"items":[[0,"04A1B2",1]],"next":1}',
        b'{"t":"punches","epoch":"00000000","from":0,"items":[[0,"04A1B2C3D4E5F607",1]],"next":1}',
        b'{"t":"punches","epoch":"00000000","from":0,"items":[[1,"04A1B2C3",1]],"next":1}',
        b'{"t":"punches","epoch":"00000000","from":5,"items":[[4,"04A1B2C3",1]],"next":6}',
        b'{"t":"punches","epoch":"00000000","from":0,'
        b'"items":[[1,"04A1B2C3",1],[1,"04A1B2C3",2]],"next":3}',
        b'{"t":"punches","epoch":"00000000","from":3,"items":[],"next":2}',
        # uint32 bounds
        b'{"t":"ack","epoch":"9f2c1a7b","next":4294967296}',
        b'{"t":"punches","epoch":"00000000","from":0,"items":[[0,"04A1B2C3",4294967296]],"next":1}',
        b'{"t":"punches","epoch":"00000000","from":0,'
        b'"items":[[0,"04A1B2C3",1000000000000000000000000000000]],"next":1}',
        b'{"t":"punches","epoch":"00000000","from":0,"items":[],"next":4294967296}',
        b'{"t":"punches","epoch":"00000000","from":4294967296,"items":[],"next":4294967296}',
        b'{"t":"welcome","proto":1,"cursor":{"epoch":"00000000","next":4294967296}}',
        b'{"t":"hello","proto":1,"id":"A","log":{"epoch":"00000000","count":4294967296}}',
    ],
)
def test_invalid_payloads(payload: bytes) -> None:
    with pytest.raises(ProtocolError) as err:
        decode(payload)
    assert err.value.close_code == 4000


def test_punches_limit() -> None:
    items = ",".join(f'[{i},"04A1B2C3",1]' for i in range(101))
    data = f'{{"t":"punches","epoch":"00000000","from":0,"items":[{items}],"next":101}}'
    with pytest.raises(ProtocolError):
        decode(data.encode())


def test_uint32_maximum_accepted() -> None:
    top = 4294967295
    data = (
        f'{{"t":"punches","epoch":"00000000","from":{top - 1},'
        f'"items":[[{top - 1},"04A1B2C3",{top}]],"next":{top}}}'
    ).encode()
    punches = decode(data)
    assert isinstance(punches, Punches)
    assert punches.items == (Punch(top - 1, "04A1B2C3", top),) and punches.next == top
    assert decode(b'{"t":"ack","epoch":"9f2c1a7b","next":4294967295}') == Ack(
        epoch="9f2c1a7b", next=top
    )


def test_empty_punches_batch() -> None:
    data = b'{"t":"punches","epoch":"00000000","from":3,"items":[],"next":7}'
    assert decode(data) == Punches(epoch="00000000", from_=3, items=(), next=7)


def test_seven_byte_uid() -> None:
    data = b'{"t":"punches","epoch":"00000000","from":0,"items":[[0,"04A1B2C3D4E5F6",1]],"next":1}'
    assert isinstance(decode(data), Punches)


def test_relay_validation() -> None:
    Relay(id=1, cmd="pulse", ms=100).validate()
    Relay(id=1, cmd="pulse", ms=10000).validate()
    Relay(id=1, cmd="open").validate()
    for bad in (
        Relay(cmd="pulse"),
        Relay(cmd="pulse", ms=99),
        Relay(cmd="pulse", ms=10001),
        Relay(cmd="toggle"),
    ):
        with pytest.raises(ValueError):
            bad.validate()
    # Decoding accepts a bad command; the device answers "invalid".
    assert decode(b'{"t":"relay","id":1,"cmd":"toggle"}') == Relay(id=1, cmd="toggle")


def test_encode_size_limit() -> None:
    with pytest.raises(ValueError):
        encode(Hello(id="X", name="x" * 5000, log=LogInfo("00000000", 0)))


@pytest.mark.parametrize(
    "message",
    [
        Ack(epoch="00000000", next=5),
        Unpair(),
        Ping(),
        Pong(id=1),
        RelayResult(id=1, ok=False, err="busy"),
        PairHello(server="S", name="N"),
        Paired(server_id="0" * 32, name="N"),
        Welcome(cursor=None),
    ],
)
def test_roundtrip(message: Message) -> None:
    assert decode(encode(message)) == message
