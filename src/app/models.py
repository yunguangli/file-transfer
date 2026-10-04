"""Core data model: plain dataclasses shared by viewmodels, protocol and views.

No Flet and no lanlink imports here — this module is the contract between
the transport, the UI state machines and the tests.
"""

from __future__ import annotations

import enum
import time
import uuid
import zlib
from dataclasses import dataclass, field
from typing import Optional


def new_id() -> str:
    return uuid.uuid4().hex


def stable_hash(key: str) -> int:
    """Deterministic hash used for avatar colors and radar slots.

    Built-in ``hash()`` is salted per process, which would reshuffle the
    radar (and everyone's avatar color) on every restart.
    """
    return zlib.crc32(key.encode("utf-8"))


class Direction(enum.Enum):
    """Which way a transfer is flowing, from *this* app's point of view."""

    OUT = "out"
    IN = "in"


class Phase(enum.Enum):
    """Life cycle of the main action area.

    ``IDLE`` and ``SELECTED`` are idle-ish states; everything from ``RINGING``
    onwards belongs to exactly one active transfer.
    """

    IDLE = "idle"  # no peer selected
    SELECTED = "selected"  # one peer selected, nothing picked yet
    PICKED = "picked"  # batch chosen, ready to send
    RINGING = "ringing"  # outgoing: waiting for the peer to answer
    INCOMING = "incoming"  # incoming offer waiting for accept/reject
    TRANSFERRING = "transferring"  # payload is moving
    DONE = "done"
    FAILED = "failed"

    @property
    def busy(self) -> bool:
        return self in (Phase.RINGING, Phase.INCOMING, Phase.TRANSFERRING)


@dataclass(frozen=True)
class PeerView:
    """A peer as the UI sees it (lanlink's `Peer` stays in the transport)."""

    id: str
    name: str
    host: str
    port: int

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Identity:
    """This app instance: who we advertise as, where we live on the LAN."""

    peer_id: str
    nickname: str
    hostname: str
    ip: str


@dataclass(frozen=True)
class Item:
    """One filesystem entry in a batch.

    `path` is relative and always uses forward slashes; for a folder it
    includes the folder's own name (``Photos/cat.jpg``), so the receiver can
    rebuild the tree exactly as it was picked.

    `source` is the absolute origin on the *sender* — used to open the file
    while streaming and deliberately never serialized on the wire.
    """

    path: str
    size: int = 0
    is_dir: bool = False
    source: str = ""

    @property
    def display_name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Batch:
    """A picked set of files/folders, ready to be offered to a peer."""

    items: tuple[Item, ...]
    batch_id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.items if not i.is_dir)

    @property
    def file_count(self) -> int:
        return sum(1 for i in self.items if not i.is_dir)

    @property
    def dir_count(self) -> int:
        return sum(1 for i in self.items if i.is_dir)

    @property
    def top_level(self) -> tuple[str, ...]:
        """Distinct first path segments — what the user actually picked."""
        seen: list[str] = []
        for item in self.items:
            root = item.path.split("/", 1)[0]
            if root not in seen:
                seen.append(root)
        return tuple(seen)

    @property
    def label(self) -> str:
        """Human label for the batch: a single name, else a count."""
        roots = self.top_level
        if not roots:
            return "Nothing selected"
        if len(roots) == 1:
            return roots[0]
        if self.dir_count:
            return f"{len(roots)} items"
        return f"{self.file_count} files"


@dataclass(frozen=True)
class IncomingOffer:
    """A peer asking to send something — the accept/reject dialog's subject."""

    batch_id: str
    sender_id: str
    sender_name: str
    sender_host: str
    label: str
    item_count: int
    file_count: int
    total: int


@dataclass(frozen=True)
class TransferProgress:
    """Instantaneous view of a running transfer (both directions)."""

    done: int
    total: int
    started_at: float
    updated_at: float
    current_path: str = ""

    @classmethod
    def start(cls, total: int) -> "TransferProgress":
        now = time.time()
        return cls(done=0, total=total, started_at=now, updated_at=now)

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.done / self.total))

    @property
    def elapsed(self) -> float:
        return max(0.0, self.updated_at - self.started_at)

    @property
    def rate(self) -> float:
        """Bytes/second, or 0 before we have enough samples to be honest."""
        if self.elapsed < 0.3:
            return 0.0
        return self.done / self.elapsed

    @property
    def eta(self) -> Optional[float]:
        """Seconds remaining, or None when the rate is still unknown."""
        rate = self.rate
        if rate <= 0:
            return None
        return max(0.0, (self.total - self.done) / rate)

    def advanced(self, done: int, *, current_path: str = "") -> "TransferProgress":
        return TransferProgress(
            done=done,
            total=self.total,
            started_at=self.started_at,
            updated_at=time.time(),
            current_path=current_path or self.current_path,
        )


@dataclass
class TransferRecord:
    """A finished transfer, shown on the status card and kept in history."""

    direction: Direction
    peer_name: str
    peer_host: str
    label: str
    item_count: int
    total_bytes: int
    started_at: float
    finished_at: float = 0.0
    ok: bool = True
    error: str = ""

    @property
    def duration(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    @property
    def verb(self) -> str:
        return "Sent" if self.direction is Direction.OUT else "Received"

    def headline(self) -> str:
        return "Transfer complete" if self.ok else "Transfer failed"
