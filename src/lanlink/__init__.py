"""lanlink — a small stdlib-only P2P transport for Python/asyncio apps.

Provides LAN peer discovery (UDP multicast + broadcast beacons with an
expiring peer table) and single-line TCP sessions (ring-delay answer, BUSY
policy, framed handshake, paced payload streaming).

Boundary rule (extraction-ready): this package is stdlib-only and must
never import Flet or anything from the host application (`models/`,
`viewmodels/`, `views/`, entry files). Application-specific semantics —
for the fax app, the FAX1 header schema and row math — live outside.

Typical use:

    discovery = Discovery(name="me", tcp_port=port, on_change=on_peers)
    await discovery.start()

    server = SessionServer(is_busy=..., on_incoming=..., accept_delay=1.8)
    port = await server.start()

    conn = await dial(peer.host, peer.port, handshake, on_waiting=...)
    await conn.send_payload(data, bytes_per_second=..., on_progress=...)
"""

from .discovery import (
    BEACON_INTERVAL,
    BEACON_PORT,
    DEFAULT_NAME,
    MCAST_GRP,
    NAME_MAX,
    PEER_EXPIRY,
    Discovery,
    Peer,
    clean_name,
)
from .errors import (
    CallRejected,
    HandshakeError,
    LanlinkError,
    LineBusy,
    PeerGone,
    TransferAborted,
)
from .session import DEFAULT_TCP_PORT, Connection, SessionServer, dial

__all__ = [
    "BEACON_INTERVAL",
    "BEACON_PORT",
    "DEFAULT_NAME",
    "DEFAULT_TCP_PORT",
    "MCAST_GRP",
    "NAME_MAX",
    "PEER_EXPIRY",
    "Discovery",
    "Peer",
    "Connection",
    "SessionServer",
    "dial",
    "clean_name",
    "LanlinkError",
    "LineBusy",
    "PeerGone",
    "CallRejected",
    "HandshakeError",
    "TransferAborted",
]
