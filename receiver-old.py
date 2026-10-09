#!/usr/bin/env python3
"""myftp receiver — a headless LanLink peer on this LAN (Python 3.5 edition).

A standalone, terminal-only version of the file-transfer app in ``src/``: it
answers calls, asks before accepting anything, and writes the payload to disk.
No GUI, no Flet, no third-party packages — standard library only, so it can be
copied onto a machine that has nothing but Python and be run as::

    python receiver-old.py

This file is a back-port of ``receiver.py`` to Python 3.5, for an old
Raspberry Pi that has only the distro interpreter. Same wire format, same
behaviour, same layout — the two files are kept line-for-line comparable on
purpose. Everything the file needs is inlined: discovery, the framed-TCP
session and the wire protocol are copies of ``src/lanlink/`` and
``src/app/protocol.py``. Each section names where it came from.

    $ python receiver-old.py
    myftp receiver · LanLink peer on this LAN
      name      : lowend-pc
      address   : 192.168.1.42
      receiving : ~/Downloads/LanLink
      waiting for a peer...

    panger-laptop (192.168.1.7) offers:
      label     : Photos
      contents  : 12 files, 3 folders · 48.2 MB
      [Y/n] accept? y
      receiving  ██████████████░░░░░░░░░  62%  18.4 MB/s  4s left
      done — 12 files, 48.2 MB in 7s
     -> ~/Downloads/LanLink/panger-laptop_20261008-140322

    waiting for a peer...

Ctrl-C stops a transfer in flight and keeps serving; a second one quits. While
a transfer runs this peer still beacons, so the sender's radar keeps showing
it — nothing here ever blocks the event loop.

Wire-compatibility notes, because this file duplicates protocol code:

* The beacon is byte-compatible with ``lanlink.discovery``: same JSON keys, same
  ``v``/``svc`` values, same multicast + broadcast fan-out, port 47555.
* ``parse_offer`` accepts exactly what ``app.protocol.make_offer`` produces, and
  ``ManifestWriter`` rebuilds the same tree, so either side can be the sender.
* Changes to the wire format need making in three places: ``receiver.py``,
  this file, and ``app/protocol.py``. The receive halves of ``receiver.py``
  and this file must agree with the app's send half.

Python 3.5 port notes (what was changed vs ``receiver.py``, and why):

* f-strings               -> ``str.format()``              (3.6)
* dataclasses             -> hand-written frozen classes   (3.7)
  for Item / Offer / Peer
* async generator         -> ``Connection.pump_payload()`` (async
  + ``async for``              generators and ``async for`` are 3.6)
* ``asyncio.run()``        -> ``get_event_loop().run_until_complete()`` (3.7)
* ``get_running_loop()``   -> ``get_event_loop()``          (3.7)
* ``create_task(name=)``   -> ``ensure_future()``           (3.7 / 3.8)
* ``current_task()``       -> ``asyncio.Task.current_task()`` (3.7)
* ``writer.wait_closed()`` -> dropped; ``close()`` is enough (3.7)
* ``loop.create_future()`` -> ``asyncio.Future(loop=loop)`` (3.5.2)
* ``Path.is_relative_to()``-> ``Path.relative_to()``        (3.9)
* ``Path.resolve()``       -> ``_resolve_lenient()``; pre-3.6 resolve()
  raises FileNotFoundError for paths that do not exist yet, which would
  break every incoming file (see the helper for details).
* annotations using builtin generics (``tuple[...]``, ``list[...]``) ->
  ``typing.Tuple`` / ``typing.List``; without the postponed-annotations
  future (3.7) 3.5 evaluates annotations at definition time.
* variable annotations (``x: T = v``) removed               (3.6)
* numeric literal ``20_000`` -> ``20000``                   (3.6)
* ``create_datagram_endpoint(sock=...)`` needs 3.5.1; on 3.5.0
  ``Discovery.start`` falls back to binding through the loop, which
  cannot pre-set SO_REUSEADDR/SO_REUSEPORT (two receivers on one host
  would then fight over the beacon port).
* The file also runs on current Python (the few APIs that were renamed
  and later removed go through the "interpreter compat" helpers at the
  top), so the transfer behaviour can be tested on a modern machine
  before copying this script to the Pi.
"""

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# ===========================================================================
# interpreter compat — this file runs on Python 3.5.x *and* on current
# Python, so the transfer behaviour can be exercised on a modern machine
# before the script is copied to the old board. Each helper prefers the
# modern name when the interpreter has it; behaviour is identical either way.
# ===========================================================================


def _current_task() -> asyncio.Task:
    """``asyncio.current_task()`` is 3.7; ``Task.current_task()`` is gone in 3.9."""
    current = getattr(asyncio, "current_task", None)
    if current is not None:
        return current()
    return asyncio.Task.current_task()


def _new_future(loop: asyncio.AbstractEventLoop) -> asyncio.Future:
    """``loop.create_future()`` is 3.5.2; ``Future(loop=...)`` is gone in 3.10."""
    create = getattr(loop, "create_future", None)
    if create is not None:
        return create()
    return asyncio.Future(loop=loop)


# ===========================================================================
# constants — from lanlink.discovery, lanlink.session and app.protocol
# ===========================================================================

# --- discovery (lanlink/discovery.py) ---
BEACON_PORT = 47555
MCAST_GRP = "239.255.42.99"
BEACON_INTERVAL = 3.0
PEER_EXPIRY = 12.0
WIRE_VERSION = 1
# A real beacon is ~130 bytes; anything larger is junk. Parsing is cheap but
# every accepted beacon can trigger a peer-table rebuild, so cap the input.
BEACON_MAX = 1024
# Capped because a peer's name is printed and echoed in a directory name.
NAME_MAX = 32
SERVICE = "lanlink"

# --- session (lanlink/session.py) ---
CONTROL_FRAME_LIMIT = 64 * 1024
DEFAULT_TCP_PORT = 47554  # stable call port so LAN firewalls can allow it
HANDSHAKE_TIMEOUT = 5.0
PAYLOAD_CHUNK = 4096

# --- protocol (app/protocol.py) ---
OFFER_TYPE = "offer"
MAX_ITEMS = 20000
MAX_PATH_LEN = 400

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

# ===========================================================================
# errors — from lanlink/errors.py
#
# LineBusy is absent on purpose: this peer sends BUSY but never receives one,
# so only the caller-side vocabulary is needed here.
# ===========================================================================


class LanlinkError(Exception):
    """Base class for all lanlink failures."""


class HandshakeError(LanlinkError):
    """The peer sent a malformed or incompatible handshake."""


class TransferAborted(LanlinkError):
    """The connection closed in the middle of a transfer."""


class ProtocolError(Exception):
    """The peer sent something malformed, inconsistent or unsafe."""


class _Cancelled(Exception):
    """Ctrl-C was pressed while this transfer was running."""


# ===========================================================================
# small helpers — from app/ipaddr.py, lanlink.discovery, app/format.py
# ===========================================================================


def hostname() -> str:
    """Short device name, used as the default advertised nickname."""
    return socket.gethostname().split(".")[0] or "this device"


def local_ip() -> str:
    """Best-effort LAN address of this machine.

    A connected UDP socket knows which source address the kernel would pick for
    the LAN without sending a single packet. Falls back to loopback when the
    machine has no route at all.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((MCAST_GRP, BEACON_PORT))  # multicast addr: never contacted
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _is_loopback(host: str) -> bool:
    return host.startswith("127.") or host in ("::1", "localhost")


def clean_name(raw: object, *, fallback: str = "unnamed") -> str:
    """Normalize a display name: single-line, printable, length-capped.

    Applied to what we advertise. Never returns an empty name; `fallback` is
    used instead.
    """
    if not isinstance(raw, str):
        return fallback
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
    return cleaned[:NAME_MAX] or fallback


# --- app/format.py, verbatim (UI copy only) --------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(count: float) -> str:
    """``1536`` -> ``1.5 KB``."""
    value = float(count)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return "{} B".format(int(value))
            if value < 100:
                return "{:.1f} {}".format(value, unit)
            return "{:.0f} {}".format(value, unit)
        value /= 1024
    return "{:.1f} {}".format(value, _UNITS[-1])


def format_rate(bytes_per_second: float) -> str:
    """``2.45e6`` -> ``2.4 MB/s``."""
    if not bytes_per_second:
        return "\u2014"
    return "{}/s".format(format_bytes(bytes_per_second))


def format_duration(seconds: float) -> str:
    """``14.2`` -> ``14s``; ``95`` -> ``1m 35s``."""
    total = int(round(seconds))
    if total < 60:
        return "{}s".format(total)
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return "{}m {:02d}s".format(minutes, secs)
    hours, minutes = divmod(minutes, 60)
    return "{}h {:02d}m".format(hours, minutes)


def plural(count: int, word: str) -> str:
    """``1 file`` / ``3 files`` — the one bit of pluralization everything uses."""
    return "{} {}{}".format(count, word, "" if count == 1 else "s")


# ===========================================================================
# manifest items — the receive subset of app/models.py
#
# @dataclass(frozen=True) in receiver.py; hand-written here because
# dataclasses are 3.7. Same field order, same keyword names, same
# equality/hash semantics, and frozen-equivalent (no attribute assignment).
# ===========================================================================


class Item:
    """One filesystem entry in a batch.

    `path` is relative and always uses forward slashes; for a folder it
    includes the folder's own name (``Photos/cat.jpg``), so the receiver can
    rebuild the tree exactly as it was picked.
    """

    __slots__ = ("path", "size", "is_dir")

    def __init__(self, path: str, size: int = 0, is_dir: bool = False) -> None:
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "is_dir", is_dir)

    def __setattr__(self, name, value):
        raise AttributeError("Item is frozen")

    def _key(self) -> Tuple[str, int, bool]:
        return (self.path, self.size, self.is_dir)

    def __repr__(self) -> str:
        return "Item(path={!r}, size={!r}, is_dir={!r})".format(*self._key())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Item):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())


class Offer:
    """A validated incoming offer."""

    __slots__ = (
        "batch_id",
        "sender_id",
        "sender_name",
        "sender_ip",
        "label",
        "items",
        "total",
    )

    def __init__(
        self,
        batch_id: str,
        sender_id: str,
        sender_name: str,
        sender_ip: str,
        label: str,
        items: Tuple[Item, ...],
        total: int,
    ) -> None:
        object.__setattr__(self, "batch_id", batch_id)
        object.__setattr__(self, "sender_id", sender_id)
        object.__setattr__(self, "sender_name", sender_name)
        object.__setattr__(self, "sender_ip", sender_ip)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "items", items)
        object.__setattr__(self, "total", total)

    def __setattr__(self, name, value):
        raise AttributeError("Offer is frozen")

    @property
    def file_count(self) -> int:
        return sum(1 for i in self.items if not i.is_dir)

    @property
    def dir_count(self) -> int:
        return sum(1 for i in self.items if i.is_dir)

    def _key(self) -> Tuple[object, ...]:
        return (
            self.batch_id,
            self.sender_id,
            self.sender_name,
            self.sender_ip,
            self.label,
            self.items,
            self.total,
        )

    def __repr__(self) -> str:
        return (
            "Offer(batch_id={!r}, sender_id={!r}, sender_name={!r}, "
            "sender_ip={!r}, label={!r}, items={!r}, total={!r})".format(*self._key())
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Offer):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())


# ===========================================================================
# wire protocol — receive subset of app/protocol.py
#
# Copied verbatim from the app so both ends agree. Any change to the format
# belongs in both files; the sender is the one that serializes.
# ===========================================================================


def normalize_relpath(raw: object) -> str:
    """Accept only safe, relative, forward-slash paths.

    A hostile peer must not be able to escape the destination folder with
    ``../../`` or an absolute path, so every manifest entry goes through here
    before it is joined onto the destination root.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_LEN:
        raise ProtocolError("bad path: {!r}".format(raw))
    unified = raw.replace("\\", "/").strip("/")
    if not unified or _WINDOWS_DRIVE.match(unified):
        raise ProtocolError("non-relative path: {!r}".format(raw))
    parts = [p for p in unified.split("/")]
    if any(p in ("", ".", "..") for p in parts):
        raise ProtocolError("unsafe path: {!r}".format(raw))
    return "/".join(parts)


def _batch_label(items: Tuple[Item, ...]) -> str:
    """Human label for a manifest: a single name, else a count.

    Mirrors ``app.models.Batch.label`` so a header with no label produces the
    same wording on both sides.
    """
    roots = []  # type: List[str]
    for item in items:
        root = item.path.split("/", 1)[0]
        if root not in roots:
            roots.append(root)
    if not roots:
        return "Nothing selected"
    if len(roots) == 1:
        return roots[0]
    if any(i.is_dir for i in items):
        return "{} items".format(len(roots))
    return "{} files".format(sum(1 for i in items if not i.is_dir))


def parse_offer(header: object) -> Offer:
    """Validate an incoming handshake, raising ProtocolError when untrusted."""
    if not isinstance(header, dict):
        raise ProtocolError("offer must be a JSON object")
    if header.get("v") != WIRE_VERSION:
        raise ProtocolError(
            "unsupported protocol version: {!r}".format(header.get("v"))
        )
    if header.get("type") != OFFER_TYPE:
        raise ProtocolError(
            "unexpected message type: {!r}".format(header.get("type"))
        )

    sender = header.get("sender")
    if not isinstance(sender, dict):
        raise ProtocolError("offer is missing sender")
    sender_id = sender.get("id")
    sender_name = sender.get("name")
    if not isinstance(sender_id, str) or not sender_id:
        raise ProtocolError("offer has no sender id")
    if not isinstance(sender_name, str):
        raise ProtocolError("offer has no sender name")
    sender_ip = sender.get("ip")
    if not isinstance(sender_ip, str):
        sender_ip = ""

    raw_items = header.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ProtocolError("offer has no items")
    if len(raw_items) > MAX_ITEMS:
        raise ProtocolError("offer has too many items")

    items = []  # type: List[Item]
    payload_total = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ProtocolError("malformed item")
        is_dir = bool(raw.get("d", False))
        size = raw.get("s", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ProtocolError("bad size for {!r}".format(raw.get("p")))
        items.append(Item(path=normalize_relpath(raw.get("p")), size=size, is_dir=is_dir))
        if not is_dir:
            payload_total += size

    total = header.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total != payload_total:
        raise ProtocolError(
            "declared total {!r} does not match manifest ({})".format(
                total, payload_total
            )
        )

    batch_id = header.get("batch")
    if not isinstance(batch_id, str) or not batch_id:
        batch_id = "unknown"

    label = header.get("label")
    if not isinstance(label, str) or not label.strip():
        label = _batch_label(tuple(items))

    return Offer(
        batch_id=batch_id,
        sender_id=sender_id,
        sender_name=clean_name(sender_name, fallback="unknown"),
        sender_ip=sender_ip,
        label=label,
        items=tuple(items),
        total=total,
    )


def _resolve_lenient(path: Path) -> Path:
    """``Path.resolve()`` that tolerates not-yet-existing paths.

    Pre-3.6 ``Path.resolve()`` is strict: it raises FileNotFoundError for any
    component that is missing, and this code resolves destination paths *before*
    the files are created. Resolves the deepest existing ancestor with the
    strict resolve() and re-appends the missing tail verbatim, so the result
    still follows symlinks where it matters for the path-escape check.
    """
    absolute = Path(os.path.abspath(str(path)))
    tail = []  # type: List[str]
    anchor = absolute
    while not anchor.exists():
        tail.append(anchor.name)
        anchor = anchor.parent
    resolved = anchor.resolve()
    for part in reversed(tail):
        resolved = resolved / part
    return resolved


def safe_destination(dest_root: Path, rel_path: str) -> Path:
    """Join a manifest path onto the destination folder without escaping it."""
    rel = normalize_relpath(rel_path)
    target = _resolve_lenient(dest_root / rel)
    root = _resolve_lenient(dest_root)
    if target != root and root not in target.parents:
        raise ProtocolError("path escapes destination: {!r}".format(rel_path))
    return target


def new_destination(base: Path, peer_name: str, *, now: str) -> Path:
    """``<base>/<peer>_<timestamp>/``, uniquified if it already exists."""
    base.mkdir(parents=True, exist_ok=True)
    safe_peer = re.sub(r"[^A-Za-z0-9._-]+", "-", peer_name).strip("-") or "peer"
    stem = "{}_{}".format(safe_peer, now)
    candidate = base / stem
    suffix = 1
    while candidate.exists():
        candidate = base / "{}_{}".format(stem, suffix)
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


class ManifestWriter:
    """Streams payload bytes into the files named by a manifest, in order.

    The transport only guarantees "N bytes"; this turns that flat stream back
    into the tree the sender picked, creating folders as it goes.
    """

    def __init__(self, dest_root: Path, items: Tuple[Item, ...]) -> None:
        self.dest_root = dest_root
        self._files = [i for i in items if not i.is_dir]
        self._index = 0
        self._handle = None
        self._remaining = 0
        self._written = 0
        for item in items:
            if item.is_dir:
                safe_destination(dest_root, item.path).mkdir(parents=True, exist_ok=True)

    @property
    def written(self) -> int:
        return self._written

    @property
    def current_path(self) -> str:
        if self._index < len(self._files):
            return self._files[self._index].path
        return ""

    def _prepare_next(self) -> None:
        """Open the next non-empty file, materialising empty ones as we pass.

        Manifests may contain zero-byte files anywhere (and may end with them),
        but the payload only carries bytes for non-empty entries — so empties
        are created at the moment the writer moves over them.
        """
        while self._handle is None and self._index < len(self._files):
            item = self._files[self._index]
            target = safe_destination(self.dest_root, item.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.size == 0:
                target.touch()
                self._index += 1
                continue
            self._handle = target.open("wb")
            self._remaining = item.size

    def write(self, chunk: bytes) -> int:
        """Consume the next bytes of the payload; returns bytes written."""
        view = memoryview(chunk)
        taken = 0
        while taken < len(view):
            self._prepare_next()
            if self._handle is None:
                raise ProtocolError("peer sent more bytes than the manifest declares")
            want = min(len(view) - taken, self._remaining)
            data = view[taken : taken + want]
            self._handle.write(data)
            taken += want
            self._remaining -= want
            self._written += want
            if self._remaining == 0:
                self._handle.close()
                self._handle = None
                self._index += 1
        return taken

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def finish(self) -> None:
        """Close and verify every declared file actually got its bytes."""
        self.close()
        while self._index < len(self._files):
            item = self._files[self._index]
            if item.size > 0:
                raise ProtocolError("payload ended before {!r}".format(item.path))
            target = safe_destination(self.dest_root, item.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            self._index += 1


# ===========================================================================
# transport — receive subset of lanlink/session.py
#
# A "call" is a single framed exchange: the caller connects and sends one JSON
# handshake, this side answers ok/reject/busy, then reads exactly N payload
# bytes and reports what it got.
# ===========================================================================


async def _send_json(writer: asyncio.StreamWriter, obj: dict) -> None:
    payload = json.dumps(obj, ensure_ascii=True).encode("utf-8")
    if len(payload) > CONTROL_FRAME_LIMIT:
        raise HandshakeError(
            "control frame too large: {} bytes".format(len(payload))
        )
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


async def _recv_json(reader: asyncio.StreamReader, *, timeout: float) -> dict:
    try:
        head = await asyncio.wait_for(reader.readexactly(4), timeout)
        size = int.from_bytes(head, "big")
        if size > CONTROL_FRAME_LIMIT:
            raise HandshakeError("control frame too large: {} bytes".format(size))
        raw = await asyncio.wait_for(reader.readexactly(size), timeout)
    except asyncio.IncompleteReadError as ex:
        raise TransferAborted("connection closed during a control frame") from ex
    except asyncio.TimeoutError as ex:
        raise TransferAborted("timed out waiting for a control frame") from ex
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        # json.JSONDecodeError is a ValueError subclass; catching ValueError
        # also covers 3.4-era json quirks (JSONDecodeError itself is 3.5+).
        raise HandshakeError("malformed control frame: {}".format(ex)) from ex
    if not isinstance(obj, dict):
        raise HandshakeError("control frame must be a JSON object")
    return obj


class Connection:
    """A post-handshake byte stream between two peers."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    @property
    def peer_host(self) -> str:
        """The socket's real remote address.

        Preferred over the address the peer puts in the offer: that one is a
        self-reported string and proves nothing.
        """
        addr = self._writer.get_extra_info("peername")
        return addr[0] if isinstance(addr, tuple) and addr else ""

    async def send_json(self, obj: dict) -> None:
        await _send_json(self._writer, obj)

    async def recv_json(self, timeout: float) -> dict:
        return await _recv_json(self._reader, timeout=timeout)

    async def pump_payload(self, total: int, on_chunk: Callable[[bytes], Any]) -> int:
        """Deliver exactly `total` raw bytes to `on_chunk` as they arrive.

        The 3.5 replacement for ``receiver.py``'s async-generator
        ``iter_payload``: async generators (and ``async for``) are 3.6, so the
        chunks are handed to a callback instead. Returns the number of bytes
        delivered.

        Raises TransferAborted (with an accurate received/total count) if the
        peer hangs up early; already-delivered chunks are not retracted.
        """
        received = 0
        while received < total:
            want = min(PAYLOAD_CHUNK, total - received)
            try:
                chunk = await self._reader.read(want)
            except OSError as ex:
                raise TransferAborted(
                    "connection lost after {}/{} payload bytes".format(received, total)
                ) from ex
            if not chunk:
                raise TransferAborted(
                    "connection closed after {}/{} payload bytes".format(received, total)
                )
            received += len(chunk)
            on_chunk(chunk)
        return received

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # No wait_closed() here: it is 3.7. close() already flushes and
        # closes the transport, and this side has always finished talking by
        # the time it runs.
        try:
            self._writer.close()
        except Exception:
            pass


class SessionServer:
    """The answering side: one line, BUSY while a call is active.

    * `is_busy()` — polled right after the handshake; when True the caller
      gets `{"reply":"busy"}` and the connection closes.
    * `validate(header)` — optional synchronous check run *before* ringing;
      raise HandshakeError to refuse silently (connection just closes).
    * `on_ringing(header)` — fires before `accept` so the UI can announce the
      incoming call.
    * `accept(header)` — optional async hook awaited before the answer;
      returning False sends `{"reply":"reject"}` and closes, which surfaces as
      CallRejected on the caller. This is what lets a user decline.
    * `on_incoming(header, conn)` — async handler that consumes the payload
      via `conn.pump_payload(...)` and returns the received byte count.
    """

    def __init__(
        self,
        *,
        is_busy: Callable[[], bool],
        on_incoming: Callable[[dict, Connection], Any],
        on_ringing: Optional[Callable[[dict], None]] = None,
        accept: Optional[Callable[[dict], Any]] = None,
        validate: Optional[Callable[[dict], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._is_busy = is_busy
        self._on_incoming = on_incoming
        self._on_ringing = on_ringing
        self._accept = accept
        self._validate = validate
        self._on_error = on_error
        self._server = None  # type: Optional[asyncio.AbstractServer]
        self._handlers = set()  # type: Set[asyncio.Task]

    async def start(self, port: int = DEFAULT_TCP_PORT) -> int:
        """Listen on 0.0.0.0 and return the bound port.

        Prefers the fixed default so cross-machine firewalls can allow a stable
        port for incoming calls; falls back to an ephemeral port when it is
        already taken (e.g. two receivers on one host).
        """
        if port:
            try:
                self._server = await asyncio.start_server(
                    self._on_client, "0.0.0.0", port
                )
                return self._server.sockets[0].getsockname()[1]
            except OSError:
                pass
        self._server = await asyncio.start_server(self._on_client, "0.0.0.0", 0)
        return self._server.sockets[0].getsockname()[1]

    async def drain(self, timeout: float = 2.0) -> None:
        """Give in-flight calls a moment to finish replying.

        Shutting the moment the last offer is handled would cancel the handler
        before it sent its final ``{"reply":"ok","bytes":N}``, leaving the
        sender convinced the transfer failed.
        """
        current = _current_task()
        pending = [t for t in self._handlers if t is not current and not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        current = _current_task()
        for task in list(self._handlers):
            if task is not current:  # never cancel the caller from inside itself
                task.cancel()
        if self._handlers:
            await asyncio.gather(
                *[t for t in list(self._handlers) if t is not current],
                return_exceptions=True,
            )

    # ---- internals ----

    def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.ensure_future(self._handle(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handler_done)

    def _handler_done(self, task: asyncio.Task) -> None:
        self._handlers.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        if self._on_error is not None:
            try:
                self._on_error(exc)
                return
            except Exception:
                pass
        print("  session error: {}".format(exc), file=sys.stderr)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = Connection(reader, writer)
        try:
            header = await conn.recv_json(HANDSHAKE_TIMEOUT)
            if self._validate is not None:
                try:
                    self._validate(header)
                except HandshakeError:
                    return  # refuse silently: the connection just closes
            if self._is_busy():
                await conn.send_json({"reply": "busy"})
                return
            if self._on_ringing is not None:
                self._on_ringing(header)
            if self._accept is not None:
                try:
                    accepted = await self._accept(header)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    accepted = False
                if not accepted:
                    try:
                        await conn.send_json({"reply": "reject"})
                    except OSError:
                        pass
                    return
            try:
                await conn.send_json({"reply": "ok"})
            except OSError:
                return  # the caller gave up while we were deciding
            received = await self._on_incoming(header, conn)
            try:
                await conn.send_json({"reply": "ok", "bytes": int(received)})
            except OSError:
                pass
        finally:
            await conn.close()


# ===========================================================================
# discovery — from lanlink/discovery.py
#
# Beacons go out three ways and the peer table expires, so a peer that leaves
# without saying goodbye disappears on its own. Kept byte-compatible with the
# app: same JSON, same version and service tags.
# ===========================================================================


class Peer:
    """A discovered lanlink endpoint."""

    __slots__ = ("id", "name", "host", "port", "last_seen")

    def __init__(
        self, id: str, name: str, host: str, port: int, last_seen: float
    ) -> None:
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "port", port)
        object.__setattr__(self, "last_seen", last_seen)

    def __setattr__(self, name, value):
        raise AttributeError("Peer is frozen")

    def _key(self) -> Tuple[str, str, str, int, float]:
        return (self.id, self.name, self.host, self.port, self.last_seen)

    def __repr__(self) -> str:
        return "Peer(id={!r}, name={!r}, host={!r}, port={!r}, last_seen={!r})".format(
            *self._key()
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Peer):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "Discovery") -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self._owner._handle_beacon(data, addr)

    def error_received(self, exc: Exception) -> None:
        pass


class Discovery:
    """Announce this endpoint and track other lanlink peers on the network."""

    def __init__(
        self,
        *,
        name: str,
        tcp_port: int,
        on_change: Callable[[List[Peer]], None],
        peer_id: Optional[str] = None,
        service: str = SERVICE,
        beacon_port: int = BEACON_PORT,
    ) -> None:
        self._id = peer_id or uuid.uuid4().hex
        self._name = name
        self._tcp_port = tcp_port
        self._service = service
        self._beacon_port = beacon_port
        self._on_change = on_change
        self._peers = {}  # type: Dict[str, Peer]
        self._transport = None  # type: Optional[asyncio.DatagramTransport]
        self._tasks = []  # type: List[asyncio.Task]
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        loop = asyncio.get_event_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _BeaconProtocol(self),
                sock=self._make_socket(),
            )
        except TypeError:
            # Python 3.5.0: create_datagram_endpoint has no sock= parameter
            # (added in 3.5.1). Let the loop bind the socket itself; the
            # pre-bind options (SO_REUSEADDR/SO_REUSEPORT) are lost, so two
            # receivers on this host would fight over the beacon port.
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _BeaconProtocol(self),
                local_addr=("", self._beacon_port),
            )
            self._configure_socket(transport.get_extra_info("socket"))
        self._transport = transport
        self._started = True
        self._tasks = [
            asyncio.ensure_future(self._beacon_loop()),
            asyncio.ensure_future(self._prune_loop()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._started = False
        self._peers.clear()

    # ---- internals ----

    @staticmethod
    def _stable_host(prev: Optional[Peer], host: str) -> str:
        """Pick one address per peer so the output does not flicker.

        A peer is reachable over several beacon paths and each arrives with its
        own source address; for a peer on this machine all of them are valid,
        so taking whichever arrived last made the address flip-flop.
        """
        if prev is None:
            return host
        if _is_loopback(host) and not _is_loopback(prev.host):
            return prev.host
        return host

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        opt = getattr(socket, "SO_REUSEPORT", None)
        if opt is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, opt, 1)
            except OSError:
                pass
        sock.bind(("", self._beacon_port))
        self._configure_socket(sock)
        return sock

    @staticmethod
    def _configure_socket(sock: socket.socket) -> None:
        """Everything that may be set after bind: broadcast + multicast groups."""
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for iface in ("0.0.0.0", "127.0.0.1"):
            mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton(iface)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass

    def _beacon_payload(self) -> bytes:
        return json.dumps(
            {
                "v": WIRE_VERSION,
                "svc": self._service,
                "id": self._id,
                "name": self._name,
                "tcp_port": self._tcp_port,
            },
            ensure_ascii=True,
        ).encode("utf-8")

    def _send_beacon(self) -> None:
        if self._transport is None:
            return
        data = self._beacon_payload()
        # 1. multicast via the default interface (other machines on the LAN)
        self._send_to(data, (MCAST_GRP, self._beacon_port))
        # 2. multicast via loopback (other instances on this host)
        try:
            sock = self._transport.get_extra_info("socket")
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton("127.0.0.1"),
            )
            self._send_to(data, (MCAST_GRP, self._beacon_port))
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton("0.0.0.0"),
            )
        except OSError:
            pass
        # 3. limited broadcast (APs that drop multicast)
        self._send_to(data, ("255.255.255.255", self._beacon_port))

    def _send_to(self, data: bytes, addr: Tuple[str, int]) -> None:
        try:
            self._transport.sendto(data, addr)  # type: ignore[union-attr]
        except OSError:
            pass

    async def _beacon_loop(self) -> None:
        while True:
            self._send_beacon()
            await asyncio.sleep(BEACON_INTERVAL)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(BEACON_INTERVAL)
            now = time.time()
            stale = [pid for pid, peer in self._peers.items() if now - peer.last_seen > PEER_EXPIRY]
            if stale:
                for pid in stale:
                    del self._peers[pid]
                self._notify()

    def _handle_beacon(self, data: bytes, addr) -> None:
        if len(data) > BEACON_MAX:
            return
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(obj, dict):
            return
        if obj.get("v") != WIRE_VERSION or obj.get("svc") != self._service:
            return
        pid = obj.get("id")
        name = obj.get("name")
        port = obj.get("tcp_port")
        if not isinstance(pid, str) or not pid or not isinstance(name, str):
            return
        if not isinstance(port, int) or not (0 < port < 65536):
            return
        if pid == self._id:  # our own beacon looping back
            return
        name = clean_name(name)  # never trust a remote display string as-is
        prev = self._peers.get(pid)
        host = self._stable_host(prev, addr[0])
        if prev is not None:
            changed = prev.host != host or prev.port != port or prev.name != name
            self._peers[pid] = Peer(pid, name, host, port, time.time())
            if not changed:
                return  # routine refresh: nothing to re-announce
        else:
            self._peers[pid] = Peer(pid, name, host, port, time.time())
        self._notify()

    def _notify(self) -> None:
        self._on_change(sorted(self._peers.values(), key=lambda p: p.name.lower()))


# ===========================================================================
# the receiver — the receive half of app/controller.py
# ===========================================================================


def _gloss_chars() -> Tuple[str, str]:
    """Block characters if the terminal can print them, else ASCII."""
    try:
        "\u2588\u2591".encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return "#", "-"
    return "\u2588", "\u2591"


class Receiver:
    """Announces this machine, answers calls, writes accepted payloads to disk."""

    def __init__(
        self,
        *,
        name: str,
        dest: Path,
        auto_accept: bool = False,
        once: bool = False,
        port: int = DEFAULT_TCP_PORT,
        beacon_port: int = BEACON_PORT,
    ) -> None:
        self.name = name
        self.dest = dest
        self.auto_accept = auto_accept
        self.once = once
        self.tcp_port = port
        self.beacon_port = beacon_port

        self.ip = local_ip()
        self.tty = sys.stdin.isatty()
        self._gloss, self._blank = _gloss_chars()

        self._server = None  # type: Optional[SessionServer]
        self._discovery = None  # type: Optional[Discovery]
        self._stop = asyncio.Event()
        self._pending_offer = None  # type: Optional[Offer]
        self._active_conn = None  # type: Optional[Connection]
        self._in_call = False
        self._cancelled = False
        self._known_peers = {}  # type: Dict[str, str]
        self._drawn = 0  # width of the progress line currently on screen
        self._started_at = 0.0
        self._last_draw = 0.0
        self._last_pct = -1
        self._served = 0
        self._in_call_once = False

    # --- lifecycle ----------------------------------------------------

    async def start(self) -> int:
        self._server = SessionServer(
            is_busy=self._is_busy,
            on_ringing=self._on_ringing,
            accept=self._accept,
            validate=self._validate,
            on_incoming=self._on_incoming,
            on_error=self._on_error,
        )
        port = await self._server.start(self.tcp_port)
        self._discovery = Discovery(
            name=self.name,
            tcp_port=port,
            beacon_port=self.beacon_port,
            on_change=self._on_peers,
        )
        await self._discovery.start()
        return port

    async def serve_forever(self) -> None:
        await self._stop.wait()

    async def close(self) -> None:
        if self._discovery is not None:
            await self._discovery.stop()
        if self._server is not None:
            await self._server.drain()
            await self._server.stop()

    @property
    def peer_id(self) -> str:
        return self._discovery._id if self._discovery is not None else ""

    @property
    def served(self) -> int:
        return self._served

    def _maybe_stop_once(self) -> None:
        """``--once``: leave once a call has been handled, however it ended."""
        if self.once and not self._in_call_once:
            self._in_call_once = True
            self._stop.set()

    def handle_sigint(self) -> None:
        """First Ctrl-C stops a transfer; the next one ends the program.

        Stopping means setting the flag *and* closing the socket — the flag
        alone would only be noticed once the next chunk arrived, and a stalled
        sender means no next chunk.
        """
        conn = self._active_conn
        if conn is not None and not self._cancelled:
            self._cancelled = True
            self._line("\n  stopping the transfer (Ctrl-C again to quit)\u2026")
            asyncio.ensure_future(conn.close())
            return
        self._line("")
        self._stop.set()

    # --- output -------------------------------------------------------

    def _line(self, text: str = "") -> None:
        """Print a line, first erasing any progress line still on screen."""
        self._clear_drawn()
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def _draw(self, text: str) -> None:
        """Paint (or repaint) the in-place progress line."""
        sys.stdout.write("\r" + text)
        sys.stdout.flush()
        self._drawn = len(text)

    def _clear_drawn(self) -> None:
        """Wipe the progress line so ordinary output starts clean."""
        if self._drawn:
            sys.stdout.write("\r" + " " * self._drawn + "\r")
            sys.stdout.flush()
            self._drawn = 0

    def _bar(self, frac: float, width: int = 26) -> str:
        filled = max(0, min(width, int(round(frac * width))))
        return self._gloss * filled + self._blank * (width - filled)

    # --- discovery ----------------------------------------------------

    def _on_peers(self, peers: List[Peer]) -> None:
        seen = {p.id: p.name for p in peers}
        for peer in peers:
            if peer.id not in self._known_peers:
                self._line("  peer online: {} ({})".format(peer.name, peer.host))
        for pid, name in self._known_peers.items():
            if pid not in seen:
                self._line("  peer offline: {}".format(name))
        self._known_peers = seen
        if peers:
            self._line(
                "  {} peer{} on this network".format(
                    len(peers), "" if len(peers) == 1 else "s"
                )
            )

    # --- call handling (the app's controller, receive half) -----------

    def _is_busy(self) -> bool:
        return self._in_call

    def _validate(self, header: dict) -> None:
        try:
            offer = parse_offer(header)
        except ProtocolError as ex:
            raise HandshakeError(str(ex)) from ex
        self._pending_offer = offer

    def _on_ringing(self, header: dict) -> None:
        offer = self._pending_offer or parse_offer(header)
        self._pending_offer = offer
        self._cancelled = False
        self._in_call = True
        self._started_at = time.monotonic()
        self._last_draw = 0.0
        self._last_pct = -1
        bits = [plural(offer.file_count, "file")]
        if offer.dir_count:
            bits.append(plural(offer.dir_count, "folder"))
        bits.append(format_bytes(offer.total))
        self._line()
        self._line(
            "{} ({}) offers:".format(offer.sender_name, offer.sender_ip or "unknown")
        )
        self._line("  label     : {}".format(offer.label))
        self._line("  contents  : {}".format(" \u00b7 ".join(bits)))
        self._line("  will land in {}".format(self.dest))

    async def _accept(self, header: dict) -> bool:
        if self.auto_accept:
            self._line("  auto-accepting")
            return True
        if not self.tty:
            # No terminal to ask on. Reject now so the sender gets its answer
            # instead of sitting out its whole consent timeout.
            self._line("  no terminal attached \u2014 rejecting (pass --auto-accept)")
            self._finish_call()
            return False
        answer = await self._ask("  [Y/n] accept? ")
        if answer is None:
            self._finish_call()
            return False
        if not answer.strip().lower() in ("", "y", "yes"):
            self._line("  rejected")
            self._finish_call()
            return False
        return True

    def _finish_call(self) -> None:
        self._in_call = False
        self._pending_offer = None

    async def _ask(self, prompt: str) -> Optional[str]:
        """Read one line from the terminal without stopping the event loop.

        ``input()`` would freeze everything, beacons included — the sender would
        watch this peer disappear off the radar for as long as the question sat
        on screen. Reading the tty through ``add_reader`` keeps the loop turning
        and, unlike a thread parked in ``input()``, stays cancellable, so
        Ctrl-C works while a prompt is open.
        """
        if self._cancelled:
            return None
        sys.stdout.write(prompt)
        sys.stdout.flush()

        loop = asyncio.get_event_loop()
        try:
            fd = sys.stdin.fileno()
        except (AttributeError, OSError, ValueError):
            return None

        # On 3.5.0 (no loop.create_future()) this is the only way to get a
        # Future bound to this loop.
        fut = _new_future(loop)  # type: asyncio.Future

        def _ready() -> None:
            try:
                line = sys.stdin.buffer.readline()
            except (OSError, ValueError):
                line = b""
            if not line:  # EOF — the user closed stdin
                _detach()
                if not fut.done():
                    fut.set_result(None)
                return
            if not fut.done():
                fut.set_result(line.decode("utf-8", "replace"))

        def _detach() -> None:
            try:
                loop.remove_reader(fd)
            except (OSError, ValueError):
                pass

        try:
            loop.add_reader(fd, _ready)
        except (OSError, ValueError):
            self._line("  cannot read the terminal \u2014 rejecting")
            return None
        try:
            return await fut
        except asyncio.CancelledError:
            raise
        finally:
            _detach()

    async def _on_incoming(self, header: dict, conn: Connection) -> int:
        try:
            offer = parse_offer(header)
        except ProtocolError as ex:
            raise HandshakeError(str(ex)) from ex

        self._active_conn = conn
        started = time.monotonic()
        dest = new_destination(
            self.dest,
            offer.sender_name,
            now=time.strftime("%Y%m%d-%H%M%S"),
        )
        writer = ManifestWriter(dest, offer.items)
        received = 0

        def _on_chunk(chunk: bytes) -> None:
            # The body of receiver.py's `async for chunk in iter_payload(...)`
            # loop, kept as a callback because 3.5 has no async generators.
            nonlocal received
            if self._cancelled:
                raise _Cancelled()
            writer.write(chunk)
            received += len(chunk)
            self._progress(received, offer.total, started)

        try:
            await conn.pump_payload(offer.total, _on_chunk)
            writer.finish()
        except (_Cancelled, TransferAborted, OSError) as ex:
            writer.close()
            self._clear_drawn()
            if isinstance(ex, _Cancelled) or self._cancelled:
                self._line(
                    "  stopped after {} ({})".format(
                        format_bytes(received),
                        format_duration(time.monotonic() - started),
                    )
                )
            else:
                self._line("  aborted: {}".format(ex))
                self._line("  whatever arrived is in {}".format(dest))
            self._maybe_stop_once()
            raise TransferAborted(str(ex)) from ex
        except Exception as ex:
            writer.close()
            raise
        finally:
            self._clear_drawn()
            self._active_conn = None
            self._cancelled = False
            self._in_call = False
            self._pending_offer = None

        self._served += 1
        self._line(
            "  done \u2014 {}, {} in {}".format(
                plural(offer.file_count, "file"),
                format_bytes(received),
                format_duration(time.monotonic() - started),
            )
        )
        self._line("  -> {}".format(dest))
        self._maybe_stop_once()
        return writer.written

    def _progress(self, done: int, total: int, started: float) -> None:
        pct = int(done * 100 / total) if total else 100
        now = time.monotonic()
        if self.tty:
            if pct == self._last_pct and now - self._last_draw < 0.2:
                return
            self._last_draw = now
            self._last_pct = pct
            rate = done / max(now - started, 1e-6)
            left = (total - done) / rate if rate > 0 else 0
            self._draw(
                "  receiving {}  {:3d}%  {}  {} left".format(
                    self._bar(done / total if total else 1.0),
                    pct,
                    format_rate(rate),
                    format_duration(left),
                )
            )
            return
        # Not a terminal: a redrawn line would be a wall of noise, so report
        # each 25% step once and nothing in between.
        if self._last_pct >= 0 and pct // 25 == self._last_pct // 25:
            return
        self._last_pct = pct
        self._line(
            "  {:3d}%  {} / {}".format(pct, format_bytes(done), format_bytes(total))
        )

    def _on_error(self, exc: BaseException) -> None:
        self._line("  call failed: {}".format(exc))
        self._active_conn = None
        self._cancelled = False
        self._in_call = False
        self._pending_offer = None


# ===========================================================================
# entry point
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="receiver-old.py",
        description="Headless myftp receiver: accept files from peers on this LAN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Nothing is written to disk for configuration: the peer id is new on\n"
            "every run, so the sender's radar shows this machine as a fresh device.\n"
            "Ctrl-C stops a transfer in flight; press it again to quit."
        ),
    )
    p.add_argument(
        "--name",
        default=None,
        help="name peers see (default: this machine's short hostname)",
    )
    p.add_argument(
        "--dest",
        default=str(Path.home() / "Downloads" / "LanLink"),
        help="where accepted transfers are written (default: %(default)s)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_TCP_PORT,
        help="TCP port for incoming calls (default: {}, "
             "falls back to an ephemeral port if taken)".format(DEFAULT_TCP_PORT),
    )
    p.add_argument(
        "--beacon-port",
        type=int,
        default=BEACON_PORT,
        help="UDP port for discovery beacons (default: {})".format(BEACON_PORT),
    )
    p.add_argument(
        "--auto-accept",
        action="store_true",
        help="accept every offer without asking (for running without a terminal)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="exit after one transfer has been handled",
    )
    return p


async def _run(args: argparse.Namespace) -> int:
    name = clean_name(args.name or hostname(), fallback="unnamed receiver")
    receiver = Receiver(
        name=name,
        dest=Path(args.dest).expanduser(),
        auto_accept=args.auto_accept,
        once=args.once,
        port=args.port,
        beacon_port=args.beacon_port,
    )

    # get_running_loop() is 3.7; inside a coroutine get_event_loop() returns
    # the running loop on every 3.5.x.
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, receiver.handle_sigint)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass  # not all platforms support signal handlers on the loop

    try:
        port = await receiver.start()
    except OSError as ex:
        print("could not start: {}".format(ex), file=sys.stderr)
        return 1

    dest = receiver.dest
    home = str(Path.home())
    try:
        # is_relative_to() is 3.9.
        dest = "~" + str(dest.relative_to(home))
    except ValueError:
        pass

    print("myftp receiver \u00b7 LanLink peer on this LAN")
    print("  name      : {}".format(receiver.name))
    print("  peer id   : {}".format(receiver.peer_id))
    print("  address   : {}".format(receiver.ip))
    print("  listening : tcp {}, beacons on udp {}".format(port, receiver.beacon_port))
    print("  receiving : {}".format(dest))
    if not receiver.tty and not receiver.auto_accept:
        print("  note      : no terminal attached; offers will be rejected")
    print("  Ctrl-C to quit")
    print()
    print("  waiting for a peer...")
    print(flush=True)

    try:
        await receiver.serve_forever()
    finally:
        await receiver.close()

    served = receiver.served
    if served:
        print("bye \u2014 {} handled".format(plural(served, "transfer")))
    else:
        print("bye")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # asyncio.run() is 3.7 and get_event_loop() no longer creates a loop on
    # current Python; new_event_loop() + set_event_loop() works everywhere.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run(args))
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally wins
        return 130
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
