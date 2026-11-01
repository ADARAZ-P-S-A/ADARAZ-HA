# ADARAZ Link protocol, version 1

ADARAZ Link connects an ADARAZ device (first: the RAZ-RCP-1 card reader / time clock) to a
server: the Home Assistant integration in this repository, a customer's own server built
with the `adaraz` Python library, or any other implementation of this document.

Design rules:

- **The device always connects out.** A server never opens a connection to a device.
  Works through NAT and does not need any listening port on the device.
- **End-to-end security does not depend on TLS.** Every connection is a Noise protocol
  session with mutual authentication and encryption. Plain `ws://` inside a LAN is safe;
  `wss://` (TLS) is supported for servers on the internet or behind proxies.
- **No shared secret ever travels on the wire.** Each side has a long-term X25519 key
  pair. Keys are exchanged once, at pairing, confirmed by a short one-time code that a
  person copies from the server to the device.
- **No punch is lost.** Delivery is driven by a cursor into the device's event log and an
  acknowledgement from the server that it stored the records durably.
- **The device decides what a server may do.** Remote relay control is off until it is
  enabled locally on the device.

Key words MUST, SHOULD and MAY are used as in RFC 2119.

## 1. Transport

- WebSocket (RFC 6455), binary messages only. Text messages MUST be rejected (close 4000).
- URL: configured on the device as the server base URL (`http://host:port`,
  `https://host:port`, `ws://...` or `wss://...`). `http` maps to `ws`, `https` to `wss`.
  The path is `/api/adaraz/ws` unless the URL already contains a path.
- The device sends the WebSocket subprotocol `adaraz.v1`. A server SHOULD echo it.
- One WebSocket message carries exactly one Noise message. The largest Noise message is
  **4096 bytes** (payload plus 16 byte authentication tag). Both sides MUST close the
  connection (4000) on a larger message.
- The first message from the device starts with one plaintext **mode byte**, followed by
  the first Noise handshake message:
  - `0x50` (`P`): pairing (section 3),
  - `0x53` (`S`): session (section 4).
  Every later message is a bare Noise message. The mode byte itself is not part of the
  prologue; each mode has its own prologue and handshake pattern (section 2), so changing
  the mode byte makes the handshake fail.

WebSocket close codes used by this protocol:

| Code | Meaning |
|---|---|
| 1000 | normal close (e.g. after pairing) |
| 1001 | liveness timeout (nothing received for 90 s) |
| 4000 | protocol error (bad framing, bad JSON, message too large, unexpected type) |
| 4001 | unknown device (session message 1 cannot be decrypted, comes from a device that is not paired, or its `hello.id` differs from the id stored at pairing) |
| 4002 | superseded (the same device opened a newer session) |
| 4003 | pairing rejected (wrong or expired code, no pairing window open) |

## 2. Cryptography

| Item | Value |
|---|---|
| Pairing | `Noise_XXpsk3_25519_AESGCM_SHA256` |
| Session | `Noise_IK_25519_AESGCM_SHA256` |
| Prologue, pairing | ASCII `ADARAZ/1 pair` (13 bytes) |
| Prologue, session | ASCII `ADARAZ/1 session` (16 bytes) |
| Static keys | X25519, 32 byte private / 32 byte public, generated on each side once |

Implementations follow the Noise Protocol Framework, revision 34. Notes for implementers:

- AESGCM nonce: 32 bits of zeros followed by the 64-bit big-endian message counter.
- Transport messages use empty associated data. Counters start at 0 in each direction;
  a session MUST be closed before a counter reaches 2^64-1 (never happens in practice:
  sessions are re-established on every reconnect).
- No rekey is used.

### Pairing code and PSK

- The server generates a **pairing code**: 12 symbols of the Crockford Base32 alphabet
  `0123456789ABCDEFGHJKMNPQRSTVWXYZ` (60 bits), shown as `XXXX-XXXX-XXXX`.
- Normalisation before use (both sides): uppercase, remove `-` and spaces, map `O`→`0`,
  `I` and `L`→`1`. A normalised code that is not 12 alphabet symbols is rejected locally.
- `psk = HKDF-SHA256(salt = "ADARAZ pairing v1", ikm = normalised code (ASCII),
  info = "psk", length = 32)`.
- A code is valid for 10 minutes, for one successful pairing, and for at most 5 failed
  handshakes. After that the server discards it.

Why 60 bits: in `XXpsk3` the device sends message 3, encrypted with the PSK, before it
knows that the server has the PSK. Someone who intercepts a pairing (wrong URL, spoofed
name or address in the LAN) can test code guesses offline against message 3. The device
waits at most 15 seconds for `paired` (section 3), so the guess has to succeed within the
code's remaining lifetime of at most 10 minutes to be of any use: about 2^59 guesses on
average, far beyond any realistic attacker. A person can additionally compare the
`server_id` shown by the server with the one shown on the device after pairing.

## 3. Pairing

Pairing introduces a device and a server to each other. It runs once, when a person adds
the device on the server (e.g. "Add integration" in Home Assistant).

1. The server opens a pairing window and shows the code and its base URL.
2. A person enters the URL and the code in the device's local admin panel.
3. The device connects with mode byte `P` and runs `Noise_XXpsk3`, device = initiator:

   | Message | Direction | Tokens | Payload |
   |---|---|---|---|
   | 1 | device → server | `e` | empty |
   | 2 | server → device | `e, ee, s, es` | `pair_hello` |
   | 3 | device → server | `s, se, psk` | `hello` |

   The server can decrypt message 3 only with the right PSK. On failure it counts a failed
   attempt and closes with 4003.
4. After message 3 the server stores the device's static public key, closes its pairing
   window and sends one transport message `paired` **within 10 seconds** of receiving
   message 3. The device gives up if `paired` has not arrived 15 seconds after it sent
   message 3. The device can decrypt it only if the server used the same PSK; only then
   does the device store the server's static public key and the URL.
5. The server closes the WebSocket (1000). The device opens a session (section 4)
   immediately.

The server stores the device's `id` from the pairing `hello`. From then on the device is
identified **only by its static public key**; the `id` in later `hello` messages MUST be
equal to the stored one, otherwise the server closes with 4001.

A device keeps at most one paired server in protocol version 1. Pairing with a new server
replaces the old one.

Message payloads (JSON, section 5):

```json
{"t": "pair_hello", "proto": 1, "server": "Home Assistant", "name": "Dom"}
{"t": "paired", "server_id": "c5e1...", "name": "Dom"}
```

`server_id` is the lowercase hex SHA-256 of the server's static public key, first 16 bytes.
The device shows it in its panel so a person can compare it with the server.

## 4. Session

1. The device connects with mode byte `S` and runs `Noise_IK`, device = initiator, using the
   server static public key stored at pairing:

   | Message | Direction | Tokens | Payload |
   |---|---|---|---|
   | 1 | device → server | `e, es, s, ss` | `hello` |
   | 2 | server → device | `e, ee, se` | `welcome` |

2. After reading message 1 the server knows the device's static public key. If it is not
   paired, the server closes with 4001.
3. The device MUST send its first transport message (`state`) right after it has read
   message 2. The server considers the session **established** only when it has decrypted
   that first transport message. Message 1 of `IK` can be replayed by anyone who recorded
   it, but a replayer cannot read message 2 or produce valid transport messages. So only
   when the new session is established does the server close an older session of the same
   device (4002), update data taken from `hello`, or act on the session in any other way.
   A server MUST close a session that is not established within 10 seconds.
4. Both sides then exchange transport messages (section 5).

Liveness: each side sends `ping` after 30 s without sending anything and closes the
connection after 90 s without receiving anything. The device reconnects with exponential
backoff from 5 s to 300 s with ±10 % jitter; it resets the backoff after a session that
lasted at least 60 s.

## 5. Messages

Every Noise payload is one UTF-8 JSON object with a string field `t` (type). Unknown
fields MUST be ignored. An unknown `t` MUST be ignored (forward compatibility), except
during the handshake, where only the types listed in sections 3 and 4 are valid.

### 5.1 Device → server

**hello** (pairing message 3, session message 1)

```json
{"t": "hello", "proto": 1, "model": "RAZ-RCP-1", "fw": "107", "id": "C00F0202",
 "name": "Wejscie glowne", "caps": ["punches", "relay"],
 "log": {"epoch": "9f2c1a7b", "count": 1234}}
```

- `id`: stable device identifier (RAZ-RCP-1: the 8 hex digit label).
- `caps`: `punches` always; `relay` only when remote relay control is enabled on the device.
- `log.epoch`: 8 lowercase hex digits identifying the current content of the event log.
  It changes whenever records can move to other indices (the log was purged, restored
  from a backup, or records were deleted). It does not change when records are appended.
- `log.count`: number of records in the log (the next index).

**punches**

```json
{"t": "punches", "epoch": "9f2c1a7b", "from": 1200,
 "items": [[1200, "04A1B2C3", 1793342337], [1202, "04FFEE11", 1793342400]],
 "next": 1203}
```

- Records in index order starting at `from`. Each item is `[index, uid_hex, unix_ts]`,
  UID uppercase hex (4 to 7 bytes).
- Indices may be skipped (deleted or damaged records are not sent). `items` may be empty:
  a message that only covers skipped indices.
- `next` is the index after the last record covered by this message, including skipped
  ones. At most 100 items per message, and the whole Noise message MUST fit in 4096 bytes.
- At most **one** `punches` message is unacknowledged at a time.

**state**

```json
{"t": "state", "relay": false, "reader_ok": true}
```

Sent after `welcome` and whenever a value changes.

**relay_result**

```json
{"t": "relay_result", "id": 7, "ok": true, "relay": true}
{"t": "relay_result", "id": 8, "ok": false, "err": "not_allowed"}
```

`err` is one of `not_allowed` (remote relay control disabled on the device), `invalid`
(bad command or parameter), `busy`.

**ping** / **pong**: `{"t": "ping", "id": 3}` → `{"t": "pong", "id": 3}` (either direction).

### 5.2 Server → device

**welcome** (session message 2)

```json
{"t": "welcome", "proto": 1, "server": "Home Assistant", "name": "Dom",
 "cursor": {"epoch": "9f2c1a7b", "next": 1200}}
```

If the log epoch changes during a session, the device waits for the `ack` of the batch in
flight and then starts again from index 0 with the new epoch.

The cursor tells the device where to start sending:

- `cursor` equal to the current epoch: send records from `next` on.
- `cursor` with a different epoch: the log was rewritten; send the whole log from index 0.
  The server deduplicates (section 6).
- `cursor: null`: the server wants no history; the device starts at its current `count`.
  A server sends `null` only on the first session after pairing, if it does not want older
  records.

**ack**

```json
{"t": "ack", "epoch": "9f2c1a7b", "next": 1203}
```

Sent after the server has durably stored everything below `next`. The device then sends
the next batch, or new punches as they happen. An `ack` with a stale epoch is ignored.

**relay**

```json
{"t": "relay", "id": 7, "cmd": "open"}
{"t": "relay", "id": 8, "cmd": "pulse", "ms": 1500}
```

`cmd`: `open` (pulse according to the device's relay settings, like an accepted card),
`on`, `off`, `pulse` with `ms` 100 to 10000. The device answers with `relay_result`.
`on`/`off` always execute and cancel a pulse in progress, so the most recent command wins;
the device MAY reject `open`/`pulse` more often than once per second with `busy`.
The device answers `not_allowed` when remote relay control is disabled on the device or
the relay is disabled in its local settings.

**unpair**: `{"t": "unpair"}`. The server forgets the device; the device deletes its pairing
and stops connecting. Sent when a person removes the device on the server.

## 6. Delivery guarantees

- A punch exists in the device's event log before the device gives any feedback.
- The device sends records from the server's cursor and never skips an index range without
  either sending its records or covering it with `next`.
- The server MUST store a batch durably before it sends `ack`. It persists the cursor
  (`epoch`, `next`) together with the data.
- Duplicates are possible after an epoch change or a lost `ack`. The key `(device id, uid,
  ts)` identifies a punch; servers deduplicate on it.
- Punches that reach the server more than 5 minutes after their `ts` are **late**. A server
  SHOULD keep them in history but SHOULD NOT trigger real-time actions for them (Home
  Assistant: the event carries `late: true`).

## 7. Device behaviour (RAZ-RCP-1)

- Keys and pairing in encrypted NVS (namespace `settings`), erased by factory reset:
  `lnk_sk` own static private key, `lnk_spk` server static public key, `lnk_url`,
  `lnk_name`, `lnk_rly` (remote relay allowed, default 0).
- Remote relay control is enabled only in the local panel, by an administrator.
- Every pairing, unpairing, change of `lnk_rly` and every relay command is written to the
  security log, as are failed pairings and failed session authentication (rate limited).
- When remote relay control is disabled or the device is unpaired, the relay returns to
  its rest state.
- The device pauses the link during heavy operations (full event download, restore, OTA)
  when it uses TLS (`wss://`); a plain `ws://` link stays connected.
