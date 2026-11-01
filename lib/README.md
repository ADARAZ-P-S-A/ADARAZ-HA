# adaraz

Python implementation of **ADARAZ Link**, the protocol that connects ADARAZ devices
(first: the RAZ-RCP-1 card reader / time clock) to a server. It is used by the Home
Assistant integration and can be used to build your own server.

- The device always connects out (WebSocket), the server never connects to a device.
- Every connection is a Noise session (`Noise_XXpsk3_25519_AESGCM_SHA256` for pairing,
  `Noise_IK_25519_AESGCM_SHA256` for sessions) with mutual authentication, so plain
  `ws://` in a LAN is safe.
- Pairing is confirmed with a one-time code that a person copies to the device.
- No punch is lost: delivery follows a cursor into the device log, and the server
  acknowledges only after it stored the records.

The wire protocol is specified in
[`docs/protocol.md`](https://github.com/ADARAZ-P-S-A/ADARAZ-HA/blob/main/docs/protocol.md).

```
pip install adaraz            # core (only depends on cryptography)
pip install "adaraz[aiohttp]" # plus the aiohttp WebSocket adapter
```

Python 3.12 or newer. The library is fully typed (`py.typed`), async, and does no
blocking I/O in the event loop. It does not import Home Assistant.

## Quick start: a server

```python
import asyncio
from aiohttp import web
from adaraz import InMemoryStorage, LinkServer, PunchReceived, generate_private_key

async def main() -> None:
    # Keep the private key and the storage persistent in real use: devices pin the
    # server's public key at pairing.
    server = LinkServer(generate_private_key(), InMemoryStorage(), server="My app", name="Office")

    def on_event(event) -> None:
        if isinstance(event, PunchReceived) and not event.punch.late:
            print("card", event.punch.uid, "at", event.punch.ts, "on", event.device.device_id)

    server.subscribe(on_event)

    app = web.Application()
    app.router.add_get("/api/adaraz/ws", server.handle_aiohttp)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8765).start()

    window = server.open_pairing()          # valid 10 minutes, one device
    print("Enter http://<host>:8765 and code", window.code, "on the device")
    device = await window                   # PairedDevice
    print("paired", device.device_id)

    await asyncio.sleep(5)
    result = await server.send_relay(device.device_id, "pulse", ms=1500)
    print("relay:", result.ok, result.err)
    await asyncio.Event().wait()

asyncio.run(main())
```

A complete standalone server with file storage and an admin HTTP API is in
[`examples/server.py`](https://github.com/ADARAZ-P-S-A/ADARAZ-HA/blob/main/examples/server.py).

## Public API

Everything below is importable from `adaraz` unless a module is named.

### Keys and codes

| Name | Description |
|---|---|
| `generate_private_key() -> bytes` | New X25519 private key (32 bytes). |
| `public_key_from_private(sk) -> bytes` | Matching public key. |
| `server_id(public_key) -> str` | Server identifier shown on the device (hex SHA-256, first 16 bytes). |
| `generate_code() -> str` | New random pairing code: 12 symbols of Crockford Base32 (60 bits), formatted `XXXX-XXXX-XXXX`. |
| `normalize_code(code) -> str` | Uppercase, remove `-` and spaces, map `O` to `0` and `I`/`L` to `1`; returns the 12 symbols without dashes. `InvalidCodeError` (a `ValueError`) if the result is not 12 alphabet symbols. |
| `format_code(code) -> str` | Normalise, then return the display form `XXXX-XXXX-XXXX`. |
| `derive_psk(code) -> bytes` | Noise PSK of a code (HKDF-SHA256 of the normalised code). Test vector: `0123-4567-89AB` gives `377690b4f263fcbe5c12a80d283e43febcb90b05326ca85477c0861940cf99a4`. |

`adaraz.codes` also exports `ALPHABET`, `CODE_LENGTH` (12) and `GROUP_LENGTH` (4).

### `LinkServer`

```python
LinkServer(private_key: bytes, storage: LinkStorage, *, server="adaraz", name="",
           ping_interval=30.0, idle_timeout=90.0, handshake_timeout=30.0,
           establish_timeout=10.0, relay_timeout=10.0, late_after=300.0, clock=time.time)
```

- `public_key`, `server_id`, `storage`, `sessions`, `pairing_windows`
- `open_pairing(timeout=600, *, import_history=True) -> PairingWindow`
  - `window.code` (show it), `window.expires_at`, `window.failures`, `window.active`
  - `await window` (or `await window.wait()`) returns the `PairedDevice`, or raises
    `PairingExpired`, `PairingFailed` (5 failed attempts, see below) or
    `PairingCancelled`
  - a **failed attempt** is a handshake that reached the code check: a well-formed
    pairing message 3 (its static key decrypts, which does not depend on the code)
    whose code protected part decrypts with none of the open windows. It counts
    against every open window, since the server cannot tell which code the device
    was given. A connection that ends, times out, or sends a malformed or oversized
    message before that point is closed (4000) without counting. Note that anyone
    who can reach the endpoint can still run 5 complete handshakes with a wrong code
    and so close an open window (this is what limits online guessing); show the
    `PairingFailed` outcome to the user and offer a new code.
  - `window.cancel()`
  - with `import_history` the device sends its whole log in the first session,
    otherwise only records made after pairing
- `await handle_aiohttp(request) -> web.WebSocketResponse`: aiohttp handler for the
  WebSocket endpoint (aiohttp is imported lazily)
- `await handle_connection(transport)`: run one connection over any `Transport`
- `get_session(device_id) -> DeviceSession | None`, `is_connected(device_id)`:
  by the id stored at pairing (a device cannot change it later, see below)
- `get_session_by_key(public_key) -> DeviceSession | None`: by the device's static
  public key, its actual identity
- `await send_relay(device_id, cmd, ms=None, *, timeout=None) -> RelayResult`:
  `cmd` is `open`, `on`, `off` or `pulse` (`ms` 100-10000). Raises `ValueError`,
  `DeviceNotConnected`, `RelayTimeout`. A refusal by the device is
  `RelayResult(ok=False, err="not_allowed" | "invalid" | "busy")`.
- `await unpair(device_id)`: forget the device and tell it to delete its pairing.
  Raises `DeviceNotFound`.
- `subscribe(listener) -> unsubscribe`: listeners get one event object (sync
  callables, or coroutine functions which are run as tasks)
- `await close()`: close all sessions and pairing windows

Events (frozen dataclasses):

| Event | Fields |
|---|---|
| `DevicePaired` | `device: PairedDevice` |
| `DeviceUnpaired` | `device` |
| `DeviceConnected` | `device`, `hello: Hello` (`fw`, `name`, `caps`, `log`); emitted when the session is established |
| `DeviceDisconnected` | `device`, `code: int \| None`; only for sessions that were established |
| `StateChanged` | `device`, `relay: bool`, `reader_ok: bool` |
| `PunchReceived` | `device`, `punch: PunchRecord` |

`PunchRecord` has `device_id`, `epoch`, `index`, `uid`, `ts`, `late`, `received_at` and
`key` (`(device_id, uid, ts)`). A late punch (arrived more than 5 minutes after `ts`)
belongs in history but should not trigger real-time actions.

`DeviceSession` (read-only): `device`, `hello`, `state`, `cursor`, `connected_at`,
`relay_allowed` (the device announced the `relay` capability).

Session rules implemented by the server:

- A device that is not paired, is paired with a different server key, or sends a
  `hello.id` different from the id stored at pairing is closed with 4001. The device
  is identified only by its static public key; `hello` updates `name`, `model` and
  `fw` of the stored record, never `device_id`.
- A session is **established** only when the server has decrypted the device's
  first transport message (the device sends `state` right after `welcome`). Session
  message 1 can be replayed by anyone who recorded it, so before that point the
  server changes nothing: no storage write, no event, no entry in `sessions` /
  `get_session`, and an older session of the same device stays up. A session that is
  not established within `establish_timeout` (10 s) is closed with 4000.
- When the new session is established, an older session of the same device is
  closed with 4002 (its `DeviceDisconnected` is emitted before the new
  `DeviceConnected`), the record is updated from `hello`, and a missing cursor is
  set to the device's current count.
- `ping` after 30 s of silence, close with 1001 after 90 s without traffic.
- `ack` only after `storage.store_punches` returned. Empty `punches` batches (only
  skipped indices) are stored and acknowledged like any other.
- Text frames, messages over 4096 bytes, bad JSON, unexpected types and integers
  outside uint32 (log indices, counts, cursor positions, punch timestamps) close
  with 4000.

### Storage

Implement `LinkStorage` (abstract base class, all methods async):

```python
class LinkStorage:
    async def list_devices(self) -> Sequence[PairedDevice]
    async def get_device(self, public_key: bytes) -> PairedDevice | None   # default uses list_devices
    async def add_device(self, device: PairedDevice) -> None              # insert or replace by key
    async def remove_device(self, public_key: bytes) -> None              # and its cursor
    async def load_cursor(self, public_key: bytes) -> Cursor | None
    async def save_cursor(self, public_key: bytes, cursor: Cursor) -> None
    async def store_punches(self, device, punches: Sequence[PunchRecord], cursor: Cursor
                            ) -> Sequence[PunchRecord]                     # returns the new ones
```

`store_punches` must make the punches and the cursor durable before it returns and
return the punches that are not duplicates; `PunchDeduplicator` helps with that.
`InMemoryStorage` is a non-persistent implementation for tests.

A device id is unique: pairing a device whose id is already known under another key
(factory reset) removes the old record and closes its session. The id is set at
pairing and never changed by a session, so `device_id` lookups are stable; key
lookups (`get_device`, `get_session_by_key`) are the primary identity.

### Transport

```python
class Transport(Protocol):
    async def recv(self) -> bytes | None       # None when closed; text frame -> ProtocolError
    async def send(self, data: bytes) -> None
    async def close(self, code: int = 1000) -> None
    @property
    def close_code(self) -> int | None
```

- `adaraz.aiohttp_ws.AiohttpTransport(ws)` wraps an aiohttp server or client
  WebSocket; `adaraz.aiohttp_ws.connect(session, base_url)` opens a client connection.
- `memory_pipe()` returns two connected in-memory transports (tests).
- `build_ws_url(base_url)` maps a configured server URL to the WebSocket URL.

### Simulated device: `adaraz.device`

`LinkDevice` is a complete device-side implementation, for testing servers without
hardware and as a reference for firmware:

```python
from adaraz.aiohttp_ws import connect
from adaraz.device import LinkDevice

device = LinkDevice(device_id="C00F0202", relay_allowed=True)
await device.pair(await connect(http, "http://127.0.0.1:8765"), "ABCD-EFGH-1234")
task = asyncio.create_task(device.run_session(await connect(http, "http://127.0.0.1:8765")))
device.punch("04A1B2C3")
await device.wait_delivered()
await device.stop()
code = await task               # WebSocket close code
```

It keeps an `EventLog` (`append`, `delete`, `purge`; deleting or purging changes the
epoch), follows the server cursor, sends `state` as its first transport message
right after `welcome`, sends batches of at most 100 records that fit in one 4096
byte Noise message (empty when every index is skipped), waits for each `ack`, and
after an epoch change waits for the `ack` in flight and starts again from index 0.
It reports `state`, executes relay commands (`not_allowed` unless `relay_allowed`
and `relay_enabled`, `on`/`off` cancel a pulse in progress, `busy` for
`open`/`pulse` within 1 s), and on `unpair` deletes its pairing and returns the relay
to rest. It does not reconnect by itself.

- `LinkDevice(..., relay_allowed=False, relay_enabled=True, ...)`: `relay_allowed`
  is the administrator's remote relay consent (announced as the `relay` capability),
  `relay_enabled` is the local relay setting (False: relay disabled).
- `await device.set_relay_allowed(allowed)`: revoking consent returns the relay to
  rest and reports `state`.
- `device.delete_record(index)`, `device.purge_log()`: change the log (new epoch)
  and restart delivery during a running session.

### Low level

`adaraz.noise` (Noise `CipherState`, `SymmetricState`, `HandshakeState`, patterns
`XXPSK3` and `IK`) and `adaraz.messages` (payload dataclasses, `encode`, `decode`) are
public too, for tools and tests. `adaraz.const` holds the protocol constants (e.g.
`ESTABLISH_TIMEOUT`, `MAX_UINT32`).

## Development

```
cd lib
pip install -e ".[test]" mypy ruff
pytest
mypy
ruff check src tests
```

The Noise implementation is tested for interoperability against the reference
library `noiseprotocol` for both patterns in both roles.

## License

MIT
