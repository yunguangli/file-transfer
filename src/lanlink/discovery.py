"""Peer discovery: multicast/broadcast beacons plus an expiring peer table.

Every lanlink endpoint periodically announces itself with a small JSON beacon
and listens for announcements from others. The peer table is pruned when
beacons stop (a peer that leaves without saying goodbye disappears after
`PEER_EXPIRY` seconds).

Transport notes (spike-verified on Linux):

* Plain UDP broadcast does NOT fan out to multiple sockets bound to the same
  port on one host on some machines, so multicast is the primary transport.
* The socket joins the multicast group on every useful interface (default +
  loopback); each beacon is emitted up to three ways:
    1. multicast via the default interface  -> other machines on the LAN
    2. multicast via loopback               -> other instances on this host
    3. limited broadcast                    -> access points that filter
       multicast but forward broadcast.
* Discovery broadcasts are only listened to here; peers are keyed by their
  stable `id` so duplicate beacon paths refresh `last_seen` instead of
  duplicating entries.

Stdlib-only: no Flet, no application imports. Extraction-ready.

Naming: an endpoint may change the name it advertises at runtime via
`set_name()`, which re-beacons immediately rather than waiting for the next
periodic tick. Names are normalized by `clean_name()` on both the send and
the receive path, so neither side can put an unbounded string into the peer
table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

BEACON_PORT = 47555
MCAST_GRP = "239.255.42.99"
BEACON_INTERVAL = 3.0
PEER_EXPIRY = 12.0
PROTOCOL_VERSION = 1
# A real beacon is ~130 bytes; anything larger is junk. Parsing is cheap but
# every accepted beacon can trigger a peer-table rebuild, so cap the input.
BEACON_MAX = 1024
# Advertised display name. Capped because the view re-materializes every
# peer entry into a control on each notification (~12/s while transferring),
# so one peer with a huge name would be amplified into a lot of wasted work.
NAME_MAX = 32
DEFAULT_NAME = "unnamed fax"

_log = logging.getLogger(__name__)


def clean_name(raw: object, *, fallback: str = DEFAULT_NAME) -> str:
    """Normalize a peer/display name: single-line, printable, length-capped.

    Applied on both ends — to what we advertise and to what we accept from a
    beacon — so the cap cannot be sidestepped by a peer sending a raw string.
    Never returns an empty name; `fallback` is used instead.
    """
    if not isinstance(raw, str):
        return fallback
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
    return cleaned[:NAME_MAX] or fallback


def _is_loopback(host: str) -> bool:
    return host.startswith("127.") or host in ("::1", "localhost")


@dataclass(frozen=True)
class Peer:
    """A discovered lanlink endpoint.

    `host` is filled from the beacon's source address today (LAN phase);
    Phase 2 can route through a relay instead without changing consumers.
    """

    id: str
    name: str
    host: str
    port: int
    last_seen: float


class _BeaconProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "Discovery") -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self._owner._handle_beacon(data, addr)

    def error_received(self, exc: Exception) -> None:
        _log.debug("discovery socket error: %s", exc)


class Discovery:
    """Announce this endpoint and track other lanlink peers on the network."""

    def __init__(
        self,
        *,
        name: str,
        tcp_port: int,
        on_change: Callable[[list[Peer]], None],
        peer_id: Optional[str] = None,
        service: str = "lanlink",
        beacon_port: int = BEACON_PORT,
    ) -> None:
        self._id = peer_id or uuid.uuid4().hex
        self._name = name
        self._tcp_port = tcp_port
        self._service = service
        self._beacon_port = beacon_port
        self._on_change = on_change
        self._peers: dict[str, Peer] = {}
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._tasks: list[asyncio.Task] = []
        self._started = False

    @property
    def peer_id(self) -> str:
        return self._id

    @property
    def started(self) -> bool:
        return self._started

    @property
    def name(self) -> str:
        return self._name

    def set_name(self, name: str) -> None:
        """Change the advertised name and re-announce immediately.

        Peers would otherwise pick the new name up from the next periodic
        beacon, leaving them staring at a stale list for up to
        BEACON_INTERVAL seconds — which reads as "renaming is broken". An
        empty/blank name is rejected in favour of the current one so a fax
        machine never becomes nameless.
        """
        cleaned = clean_name(name, fallback=self._name)
        if cleaned == self._name:
            return
        self._name = cleaned
        self._send_beacon()

    def peers(self) -> list[Peer]:
        """Known peers, sorted by display name."""
        return sorted(self._peers.values(), key=lambda p: p.name.lower())

    def get(self, peer_id: str) -> Optional[Peer]:
        return self._peers.get(peer_id)

    async def start(self) -> None:
        if self._started:
            return
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _BeaconProtocol(self),
            sock=self._make_socket(),
        )
        self._transport = transport
        self._started = True
        self._tasks = [
            asyncio.create_task(self._beacon_loop(), name="lanlink-beacon"),
            asyncio.create_task(self._prune_loop(), name="lanlink-prune"),
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
        """Pick one address per peer so the row does not flicker.

        A peer is reachable over several beacon paths, and each arrives with
        its own source address: LAN multicast reports the LAN IP, loopback
        multicast reports 127.0.0.1, broadcast reports the LAN IP again. For a
        peer on this same machine all of them are valid routes to it, so
        taking whichever arrived last made the displayed address flip-flop on
        every beacon — and each flip counted as a change, re-rendering the peer
        table for nothing. Once a routable address is known, loopback never
        displaces it (the reverse is kept, so a peer that genuinely moves off
        loopback still updates).
        """
        if prev is None:
            return host
        if _is_loopback(host) and not _is_loopback(prev.host):
            return prev.host
        return host

    def _make_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        for attr in ("SO_REUSEPORT",):  # allow several instances on one host
            opt = getattr(socket, attr, None)
            if opt is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, opt, 1)
                except OSError:
                    pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", self._beacon_port))
        for iface in ("0.0.0.0", "127.0.0.1"):
            mreq = socket.inet_aton(MCAST_GRP) + socket.inet_aton(iface)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as ex:
                _log.debug("multicast join on %s failed: %s", iface, ex)
        return sock

    def _beacon_payload(self) -> bytes:
        return json.dumps(
            {
                "v": PROTOCOL_VERSION,
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
        # 1. multicast via default interface (other machines on the LAN)
        self._send_to(data, (MCAST_GRP, self._beacon_port))
        # 2. multicast via loopback (other instances on this host)
        try:
            self._transport.get_extra_info("socket").setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton("127.0.0.1"),
            )
            self._send_to(data, (MCAST_GRP, self._beacon_port))
            self._transport.get_extra_info("socket").setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("0.0.0.0")
            )
        except OSError as ex:
            _log.debug("loopback multicast send failed: %s", ex)
        # 3. limited broadcast (APs that drop multicast)
        self._send_to(data, ("255.255.255.255", self._beacon_port))

    def _send_to(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self._transport.sendto(data, addr)  # type: ignore[union-attr]
        except OSError as ex:
            _log.debug("beacon send to %s failed: %s", addr, ex)

    async def _beacon_loop(self) -> None:
        while True:
            self._send_beacon()
            await asyncio.sleep(BEACON_INTERVAL)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(BEACON_INTERVAL)
            now = time.time()
            stale = [
                pid
                for pid, peer in self._peers.items()
                if now - peer.last_seen > PEER_EXPIRY
            ]
            if stale:
                for pid in stale:
                    del self._peers[pid]
                self._notify()

    def _handle_beacon(self, data: bytes, addr) -> None:
        if len(data) > BEACON_MAX:
            return  # a real beacon is ~130 bytes; refuse to parse the rest
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(obj, dict):
            return
        if obj.get("v") != PROTOCOL_VERSION or obj.get("svc") != self._service:
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
                return  # routine refresh: table view does not need re-render
        else:
            self._peers[pid] = Peer(pid, name, host, port, time.time())
        self._notify()

    def _notify(self) -> None:
        try:
            self._on_change(self.peers())
        except Exception:
            _log.exception("discovery on_change callback failed")
