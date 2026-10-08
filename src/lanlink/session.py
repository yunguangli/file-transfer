"""TCP sessions: answer incoming peers (ring delay + BUSY) and dial peers
with paced payload streaming.

A "call" is a single framed exchange:

    caller                                   answerer
      │  connect                                 │
      │──── JSON handshake (4-byte len + UTF-8) ─▶│  validate
      │                                          │  busy? ──▶ {"reply":"busy"} + close
      │                                          │  on_ringing hook, then the optional
      │                                          │  async `accept` hook (user consent):
      │                                          │  declined ──▶ {"reply":"reject"} + close
      │                                          │  accept_delay sleep
      │◀─────────────── {"reply":"ok"} ──────────│
      │──── raw payload (declared length N) ────▶│  read N bytes
      │◀────── {"reply":"ok","bytes":N} ─────────│

Consent is transport-level on purpose: `accept` is an arbitrary coroutine, so
lanlink never learns *what* is being offered — the app decides, the wire only
carries ok/reject.

Pacing: `send_payload(bytes_per_second=...)` transmits in small (20 ms)
quanta to simulate a slow line so the receiver's line-by-line fill stays
continuous; `None` blasts at full speed ("turbo"). The answerer's
`accept_delay` simulates ring time *before* the ok reply, so the caller sits
on RINGING while waiting.

Stdlib-only: no Flet, no application imports. Extraction-ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Awaitable, Callable, Optional

from .errors import CallRejected, HandshakeError, LineBusy, PeerGone, TransferAborted

CONTROL_FRAME_LIMIT = 64 * 1024
DEFAULT_TCP_PORT = 47554   # stable call port so LAN firewalls can allow it
                           # (beacons use DEFAULT_TCP_PORT + 1 = 47555)
HANDSHAKE_TIMEOUT = 5.0
REPLY_TIMEOUT = 15.0   # covers the ring delay before the answer
FINAL_TIMEOUT = 30.0   # receiver renders the finished page before replying
PAYLOAD_CHUNK = 4096
TICK = 0.02            # pacing tick (seconds) — small quanta keep the
                       # receiver's line-by-line fill looking continuous

_log = logging.getLogger(__name__)


async def _send_json(writer: asyncio.StreamWriter, obj: dict) -> None:
    payload = json.dumps(obj, ensure_ascii=True).encode("utf-8")
    if len(payload) > CONTROL_FRAME_LIMIT:
        raise HandshakeError(f"control frame too large: {len(payload)} bytes")
    writer.write(len(payload).to_bytes(4, "big") + payload)
    await writer.drain()


async def _recv_json(reader: asyncio.StreamReader, *, timeout: float) -> dict:
    try:
        head = await asyncio.wait_for(reader.readexactly(4), timeout)
        size = int.from_bytes(head, "big")
        if size > CONTROL_FRAME_LIMIT:
            raise HandshakeError(f"control frame too large: {size} bytes")
        raw = await asyncio.wait_for(reader.readexactly(size), timeout)
    except asyncio.IncompleteReadError as ex:
        raise TransferAborted("connection closed during a control frame") from ex
    except asyncio.TimeoutError as ex:
        raise TransferAborted("timed out waiting for a control frame") from ex
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise HandshakeError(f"malformed control frame: {ex}") from ex
    if not isinstance(obj, dict):
        raise HandshakeError("control frame must be a JSON object")
    return obj


class Connection:
    """A post-handshake byte stream between two peers."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def send_json(self, obj: dict) -> None:
        await _send_json(self._writer, obj)

    async def recv_json(self, timeout: float = REPLY_TIMEOUT) -> dict:
        return await _recv_json(self._reader, timeout=timeout)

    async def send_payload(
        self,
        data: bytes,
        *,
        bytes_per_second: Optional[int] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Stream exactly `len(data)` bytes; the peer reads that same count.

        `bytes_per_second=None` sends everything at once (turbo). Otherwise
        the stream is cut into TICK-sized quanta so the transfer takes on the
        order of len(data)/bytes_per_second seconds.
        """
        total = len(data)
        if bytes_per_second is None:
            self._writer.write(data)
            await self._writer.drain()
            if on_progress is not None:
                on_progress(total, total)
            return
        quota = max(int(bytes_per_second * TICK), 1)
        sent = 0
        while sent < total:
            chunk = min(quota, total - sent)
            self._writer.write(data[sent : sent + chunk])
            await self._writer.drain()
            sent += chunk
            if on_progress is not None:
                on_progress(sent, total)
            if sent < total:
                await asyncio.sleep(TICK)

    async def iter_payload(
        self,
        total: int,
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> AsyncIterator[bytes]:
        """Yield exactly `total` raw bytes as they arrive from the wire.

        Streaming consumer: callers can visualize/process each chunk the
        moment it lands instead of waiting for the whole payload. Raises
        TransferAborted (with an accurate received/total count) if the peer
        hangs up early; already-yielded chunks are not retracted.
        """
        received = 0
        while received < total:
            want = min(PAYLOAD_CHUNK, total - received)
            try:
                chunk = await self._reader.read(want)
            except OSError as ex:
                raise TransferAborted(
                    f"connection lost after {received}/{total} payload bytes"
                ) from ex
            if not chunk:
                raise TransferAborted(
                    f"connection closed after {received}/{total} payload bytes"
                )
            received += len(chunk)
            if on_progress is not None:
                on_progress(received, total)
            yield chunk

    async def read_payload(
        self,
        total: int,
        *,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        """Collect the whole payload (convenience wrapper over iter_payload)."""
        buf = bytearray()
        async for chunk in self.iter_payload(total, on_progress=on_progress):
            buf.extend(chunk)
        return bytes(buf)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


async def dial(
    host: str,
    port: int,
    handshake: dict,
    *,
    connect_timeout: float = 5.0,
    reply_timeout: float = REPLY_TIMEOUT,
    on_connected: Optional[Callable[[Connection], None]] = None,
    on_waiting: Optional[Callable[[], None]] = None,
) -> Connection:
    """Call a peer: connect, present `handshake`, wait for the answer.

    Raises LineBusy if the peer is already on a call, CallRejected if the user
    on the other side declined it, PeerGone if the host is unreachable,
    TransferAborted if it hangs up before answering.
    `on_waiting` fires once the handshake is sent (still waiting for the
    reply) so the UI can show "Ringing...".
    `on_connected` fires as soon as the socket is up — before the handshake —
    handing out the live `Connection`. `dial` only returns *after* the answer,
    so a caller that needs to hang up mid-wait (the user cancels a ringing
    call) has no other way to reach the socket; closing it makes the pending
    read fail with TransferAborted. Handlers run inside the failure path, so
    throwing from one still closes the connection.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), connect_timeout
        )
    except (OSError, asyncio.TimeoutError) as ex:
        raise PeerGone(f"could not reach {host}:{port} ({ex})") from ex
    conn = Connection(reader, writer)
    try:
        if on_connected is not None:
            on_connected(conn)
        await conn.send_json(handshake)
        if on_waiting is not None:
            on_waiting()
        try:
            reply = await conn.recv_json(timeout=reply_timeout)
        except TransferAborted as ex:
            raise TransferAborted(f"{host}:{port} hung up before answering") from ex
    except BaseException:
        await conn.close()
        raise
    if reply.get("reply") == "busy":
        await conn.close()
        raise LineBusy(f"{host}:{port} is busy (line in use)")
    if reply.get("reply") == "reject":
        await conn.close()
        raise CallRejected(f"{host}:{port} declined the call")
    if reply.get("reply") != "ok":
        await conn.close()
        raise HandshakeError(f"unexpected answer: {reply!r}")
    return conn


class SessionServer:
    """The answering side: one line, BUSY while a call is active.

    * `is_busy()` — polled right after the handshake; when True the caller
      gets `{"reply":"busy"}` and the connection closes.
    * `validate(header)` — optional synchronous check run *before* ringing;
      raise HandshakeError to refuse silently (connection just closes).
    * `on_ringing(header)` — fires before `accept_delay` so the UI can show
      an incoming call while the "phone rings".
    * `accept(header)` — optional async hook awaited after ringing and before
      the answer; returning False sends `{"reply":"reject"}` and closes, which
      surfaces as CallRejected on the caller. This is what lets a user decline
      a call; omit it and every well-formed call is answered.
    * `on_incoming(header, conn)` — async handler that consumes the payload
      via `conn.read_payload(...)` and returns the received byte count
      (an int) so the server can send the final ok.
    """

    def __init__(
        self,
        *,
        is_busy: Callable[[], bool],
        on_incoming: Callable[[dict, Connection], Awaitable[int]],
        accept_delay: float = 0.0,
        on_ringing: Optional[Callable[[dict], None]] = None,
        accept: Optional[Callable[[dict], Awaitable[bool]]] = None,
        validate: Optional[Callable[[dict], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._is_busy = is_busy
        self._on_incoming = on_incoming
        self._accept_delay = accept_delay
        self._on_ringing = on_ringing
        self._accept = accept
        self._validate = validate
        self._on_error = on_error
        self._server: Optional[asyncio.AbstractServer] = None
        self._handlers: set[asyncio.Task] = set()

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self, port: int = DEFAULT_TCP_PORT) -> int:
        """Listen on 0.0.0.0 and return the bound port.

        Prefers the fixed `DEFAULT_TCP_PORT` so cross-machine firewalls can
        allow a stable port for incoming calls; falls back to an ephemeral
        port when it is already taken (e.g. a second instance on this host).
        Pass `port=0` to go straight to an ephemeral port.
        """
        if port:
            try:
                self._server = await asyncio.start_server(
                    self._on_client, "0.0.0.0", port
                )
                return self._server.sockets[0].getsockname()[1]
            except OSError:
                _log.debug("port %s taken, falling back to ephemeral", port)
        self._server = await asyncio.start_server(self._on_client, "0.0.0.0", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.cancel_active()
        if self._handlers:
            await asyncio.gather(*list(self._handlers), return_exceptions=True)

    def cancel_active(self) -> None:
        """Hang up: cancel whatever call handler is currently running."""
        for task in list(self._handlers):
            task.cancel()

    # ---- internals ----

    def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer), name="lanlink-call")
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
                _log.exception("session on_error callback failed")
        _log.error("session handler failed", exc_info=exc)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = Connection(reader, writer)
        try:
            header = await conn.recv_json(timeout=HANDSHAKE_TIMEOUT)
            if self._validate is not None:
                try:
                    self._validate(header)
                except HandshakeError:
                    _log.debug("refusing handshake: %s", header)
                    return
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
                    _log.exception("session accept callback failed; rejecting")
                    accepted = False
                if not accepted:
                    try:
                        await conn.send_json({"reply": "reject"})
                    except OSError:
                        _log.debug("could not deliver reject; caller is gone")
                    return
            if self._accept_delay > 0:
                await asyncio.sleep(self._accept_delay)
            try:
                await conn.send_json({"reply": "ok"})
            except OSError:
                # The caller gave up while the phone was still ringing.
                _log.debug("caller hung up before the answer; dropping call")
                return
            received = await self._on_incoming(header, conn)
            n = received if isinstance(received, int) else int(header.get("bytes", 0))
            try:
                await conn.send_json({"reply": "ok", "bytes": n})
            except OSError:
                # Caller hung up after the payload — it already has the
                # outcome it needs (or never will); nothing left to report.
                _log.debug("could not deliver final reply; caller is gone")
        finally:
            await conn.close()
