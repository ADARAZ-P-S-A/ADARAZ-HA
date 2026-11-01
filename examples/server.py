"""Standalone ADARAZ Link server built with the ``adaraz`` library (no Home Assistant).

It accepts ADARAZ devices (e.g. the RAZ-RCP-1 reader), stores their punches in a TSV
file and lets you drive their relay from a small local HTTP API.

Install and run::

    pip install "adaraz[aiohttp]"
    python examples/server.py --data ./adaraz-data --port 8765 --pair

``--pair`` (or an empty device list) opens a pairing window on start and prints the
code. In the device's admin panel enter the server URL (``http://<this host>:8765``)
and the code. Pairing windows last 10 minutes and accept one device.

Files in ``--data``:

* ``state.json``: the server key, paired devices and delivery cursors. Keep it
  private (it holds the server's private key) and back it up: devices pin the key.
* ``punches.tsv``: one line per punch, columns ``received_at device_id uid ts late
  epoch index`` (``late`` is 1 when the punch arrived more than 5 minutes after it
  was made). Duplicates are filtered.

Admin HTTP API, on ``--admin-host``/``--admin-port`` (default ``127.0.0.1:8766``;
it has no authentication, do not expose it)::

    curl localhost:8766/                                   # status
    curl -X POST localhost:8766/pair                        # open a pairing window
    curl -X POST 'localhost:8766/relay/C00F0202?cmd=open'
    curl -X POST 'localhost:8766/relay/C00F0202?cmd=pulse&ms=1500'
    curl -X POST localhost:8766/unpair/C00F0202

``cmd`` is ``open``, ``on``, ``off`` or ``pulse`` (``ms`` 100-10000). The device must
have remote relay control enabled in its own panel, otherwise it answers
``not_allowed``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aiohttp import web

from adaraz import (
    AdarazError,
    Cursor,
    DeviceConnected,
    DeviceDisconnected,
    DevicePaired,
    DeviceUnpaired,
    LinkEvent,
    LinkServer,
    LinkStorage,
    PairedDevice,
    PairingError,
    PunchDeduplicator,
    PunchReceived,
    PunchRecord,
    StateChanged,
    generate_private_key,
)

_LOGGER = logging.getLogger("adaraz.example")


class FileStorage(LinkStorage):
    """Devices and cursors in a JSON file, punches appended to a TSV file.

    All file I/O runs in a worker thread so the event loop never blocks.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.state_path = directory / "state.json"
        self.punches_path = directory / "punches.tsv"
        self.state: dict[str, Any] = {}
        self.dedup = PunchDeduplicator()
        self._lock = asyncio.Lock()

    # -- loading -------------------------------------------------------------

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text("utf-8"))
        else:
            self.state = {"private_key": generate_private_key().hex()}
            self._write_state_sync()
        self.state.setdefault("devices", {})
        self.state.setdefault("cursors", {})
        if self.punches_path.exists():
            with self.punches_path.open(encoding="utf-8") as fh:
                for line in fh:
                    cols = line.rstrip("\n").split("\t")
                    if len(cols) >= 4 and cols[3].isdigit():
                        self.dedup.add((cols[1], cols[2], int(cols[3])))

    @property
    def private_key(self) -> bytes:
        return bytes.fromhex(self.state["private_key"])

    # -- writing -------------------------------------------------------------

    def _write_state_sync(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)

    def _append_punches_sync(self, punches: Sequence[PunchRecord]) -> None:
        with self.punches_path.open("a", encoding="utf-8") as fh:
            for p in punches:
                fh.write(
                    f"{p.received_at:.0f}\t{p.device_id}\t{p.uid}\t{p.ts}\t"
                    f"{int(p.late)}\t{p.epoch}\t{p.index}\n"
                )
            fh.flush()
            os.fsync(fh.fileno())

    async def _save(self) -> None:
        await asyncio.to_thread(self._write_state_sync)

    # -- LinkStorage ---------------------------------------------------------

    async def list_devices(self) -> Sequence[PairedDevice]:
        return [PairedDevice.from_dict(d) for d in self.state["devices"].values()]

    async def add_device(self, device: PairedDevice) -> None:
        async with self._lock:
            self.state["devices"][device.public_key.hex()] = device.to_dict()
            await self._save()

    async def remove_device(self, public_key: bytes) -> None:
        async with self._lock:
            self.state["devices"].pop(public_key.hex(), None)
            self.state["cursors"].pop(public_key.hex(), None)
            await self._save()

    async def load_cursor(self, public_key: bytes) -> Cursor | None:
        data = self.state["cursors"].get(public_key.hex())
        return None if data is None else Cursor(data["epoch"], data["next"])

    async def save_cursor(self, public_key: bytes, cursor: Cursor) -> None:
        async with self._lock:
            self.state["cursors"][public_key.hex()] = cursor.to_json()
            await self._save()

    async def store_punches(
        self, device: PairedDevice, punches: Sequence[PunchRecord], cursor: Cursor
    ) -> Sequence[PunchRecord]:
        async with self._lock:
            new: list[PunchRecord] = []
            seen: set[tuple[str, str, int]] = set()
            for p in punches:
                if p.key not in self.dedup and p.key not in seen:
                    seen.add(p.key)
                    new.append(p)
            if new:
                await asyncio.to_thread(self._append_punches_sync, new)
                for p in new:
                    self.dedup.add(p.key)
            # Punches first, then the cursor: a crash in between only causes a resend,
            # which the deduplication absorbs.
            self.state["cursors"][device.public_key.hex()] = cursor.to_json()
            await self._save()
        return new


def print_event(event: LinkEvent) -> None:
    match event:
        case PunchReceived(device=device, punch=punch):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(punch.ts))
            late = " (late)" if punch.late else ""
            print(f"punch  {device.device_id} {punch.uid} {when}{late}")
        case DeviceConnected(device=device, hello=hello):
            print(f"online {device.device_id} {hello.name!r} fw {hello.fw} caps {hello.caps}")
        case DeviceDisconnected(device=device, code=code):
            print(f"offline {device.device_id} (close {code})")
        case StateChanged(device=device, relay=relay, reader_ok=reader_ok):
            print(f"state  {device.device_id} relay={relay} reader_ok={reader_ok}")
        case DevicePaired(device=device):
            print(f"paired {device.device_id} {device.name!r}")
        case DeviceUnpaired(device=device):
            print(f"unpaired {device.device_id}")


def open_window(server: LinkServer, history: bool, base_url: str) -> dict[str, Any]:
    window = server.open_pairing(import_history=history)
    print(f"\nPairing code: {window.code}   server URL: {base_url}   (valid 10 minutes)")
    print(f"Server id: {server.server_id}\n")

    async def report() -> None:
        try:
            device = await window
            print(f"pairing finished: {device.device_id}")
        except PairingError as err:
            print(f"pairing window closed: {type(err).__name__}")

    task = asyncio.ensure_future(report())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"code": window.code, "expires_at": window.expires_at}


_background: set[asyncio.Future[None]] = set()


def admin_app(server: LinkServer, history: bool, base_url: str) -> web.Application:
    async def status(_: web.Request) -> web.Response:
        devices = await server.storage.list_devices()
        out = []
        for device in devices:
            session = server.get_session(device.device_id)
            out.append(
                {
                    **device.to_dict(),
                    "connected": session is not None,
                    "relay_allowed": bool(session and session.relay_allowed),
                    "state": None
                    if not session or not session.state
                    else {
                        "relay": session.state.relay,
                        "reader_ok": session.state.reader_ok,
                    },
                }
            )
        return web.json_response({"server_id": server.server_id, "devices": out})

    async def pair(_: web.Request) -> web.Response:
        return web.json_response(open_window(server, history, base_url))

    async def relay(request: web.Request) -> web.Response:
        cmd = request.query.get("cmd", "open")
        ms = int(request.query["ms"]) if "ms" in request.query else None
        try:
            result = await server.send_relay(request.match_info["device_id"], cmd, ms)
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        except AdarazError as err:
            return web.json_response({"error": type(err).__name__}, status=409)
        return web.json_response({"ok": result.ok, "relay": result.relay, "err": result.err})

    async def unpair(request: web.Request) -> web.Response:
        try:
            await server.unpair(request.match_info["device_id"])
        except AdarazError as err:
            return web.json_response({"error": type(err).__name__}, status=404)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", status)
    app.router.add_post("/pair", pair)
    app.router.add_post("/relay/{device_id}", relay)
    app.router.add_post("/unpair/{device_id}", unpair)
    return app


async def main(args: argparse.Namespace) -> None:
    storage = FileStorage(Path(args.data))
    await storage.load()
    server = LinkServer(storage.private_key, storage, server="adaraz example", name=args.name)
    server.subscribe(print_event)

    device_app = web.Application()
    device_app.router.add_get("/api/adaraz/ws", server.handle_aiohttp)
    runners = []
    for app, host, port in (
        (device_app, args.host, args.port),
        (admin_app(server, not args.no_history, args.url), args.admin_host, args.admin_port),
    ):
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        runners.append(runner)
    print(
        f"ADARAZ Link server on {args.host}:{args.port}, admin API on "
        f"{args.admin_host}:{args.admin_port}, data in {args.data}"
    )
    if args.pair or not await storage.list_devices():
        open_window(server, not args.no_history, args.url)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()
        for runner in runners:
            await runner.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone ADARAZ Link server")
    parser.add_argument("--data", default="adaraz-data", help="data directory")
    parser.add_argument("--host", default="0.0.0.0", help="address for devices")
    parser.add_argument("--port", type=int, default=8765, help="port for devices")
    parser.add_argument("--admin-host", default="127.0.0.1")
    parser.add_argument("--admin-port", type=int, default=8766)
    parser.add_argument("--name", default="adaraz", help="server name shown on devices")
    parser.add_argument("--url", help="server URL to show with the pairing code")
    parser.add_argument("--pair", action="store_true", help="open a pairing window on start")
    parser.add_argument(
        "--no-history", action="store_true", help="do not import punches made before pairing"
    )
    args = parser.parse_args()
    if args.url is None:
        args.url = f"http://<this host>:{args.port}"
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(parse_args()))
