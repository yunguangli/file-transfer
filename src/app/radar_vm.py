"""Radar screen state: who is nearby, who is picked, what we call ourselves.

Pure state + deterministic layout maths — no Flet, no lanlink — so both can
be unit-tested without a window or a network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Optional, Sequence

from .models import Identity, PeerView, stable_hash

# Three concentric rings with four seats each = twelve stable radar seats.
RING_RADII = (0.26, 0.33, 0.40)  # fraction of the radar diameter
RING_OFFSET_DEG = (0, 30, 60)  # staggered so seats never line up radially
SEATS_PER_RING = 4
OVERFLOW_RADIUS_STEP = 0.06  # extra radius per colliding peer, as fraction
MAX_SEAT_RADIUS = 0.46  # never seat anyone past this (fraction of the size)


@dataclass(frozen=True)
class RadarState:
    """Immutable snapshot handed to the views on every change."""

    identity: Identity
    peers: tuple[PeerView, ...] = ()
    selected_id: Optional[str] = None
    scanning: bool = True

    @property
    def selected(self) -> Optional[PeerView]:
        if self.selected_id is None:
            return None
        for peer in self.peers:
            if peer.id == self.selected_id:
                return peer
        return None

    @property
    def peer_count(self) -> int:
        return len(self.peers)

    @property
    def title(self) -> str:
        """Top-bar copy, mirroring the mock's 'Scan in Progress'."""
        if self.selected is not None:
            return "Peer Selected"
        if self.peers:
            return "Choose a Peer"
        return "Scan in Progress"

    @property
    def status_line(self) -> str:
        if not self.peers:
            return "Looking for devices on this network…"
        if self.selected is not None:
            return f"Connected to {self.selected.name}"
        count = self.peer_count
        return f"{count} nearby peer{'s' if count != 1 else ''}"


def peer_slots(peer_ids: Sequence[str], size: float) -> dict[str, tuple[float, float]]:
    """Assign every peer a seat on the radar (center coordinates).

    Seats are derived from a stable hash of the peer id, so a peer keeps its
    place while others come and go — and collisions are resolved in id order
    so two peers never draw on top of each other.

    There are only twelve seats, so a larger peer list is handled by parking
    the surplus on their probed seat and pushing it outwards, pinned to
    :data:`MAX_SEAT_RADIUS`. The probe loop is bounded — without that, a
    network with more than twelve peers left every seat taken and this
    function never returned (freezing the UI with it).
    """
    cx = cy = size / 2.0
    seats: list[tuple[float, float]] = []
    for ring, radius in enumerate(RING_RADII):
        offset = RING_OFFSET_DEG[ring % len(RING_OFFSET_DEG)]
        for k in range(SEATS_PER_RING):
            angle = math.radians((k * 360 / SEATS_PER_RING + offset) % 360)
            seats.append((radius * size, angle))

    taken: set[int] = set()
    stacked: dict[int, int] = {}
    placements: dict[str, tuple[float, float]] = {}
    total = len(seats)
    for peer_id in sorted(peer_ids):
        index = stable_hash(peer_id) % total
        overflow = 0
        while index in taken and overflow < total:
            overflow += 1
            index = (index + 1) % total
        if index in taken:
            # Every seat is occupied: stack onto the probed one instead of
            # probing forever.
            overflow = total + stacked.get(index, 0)
        taken.add(index)
        stacked[index] = stacked.get(index, 0) + 1

        radius, angle = seats[index]
        radius = min(radius + OVERFLOW_RADIUS_STEP * size * overflow, MAX_SEAT_RADIUS * size)
        placements[peer_id] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
    return placements


class RadarViewModel:
    """Owns :class:`RadarState` and notifies a listener after each change."""

    def __init__(
        self,
        identity: Identity,
        *,
        on_change: Optional[Callable[[RadarState], None]] = None,
    ) -> None:
        self._state = RadarState(identity=identity)
        self._on_change = on_change

    @property
    def state(self) -> RadarState:
        return self._state

    @property
    def identity(self) -> Identity:
        return self._state.identity

    @property
    def selected(self) -> Optional[PeerView]:
        return self._state.selected

    def _emit(self) -> RadarState:
        if self._on_change is not None:
            self._on_change(self._state)
        return self._state

    def _set(self, **changes) -> RadarState:
        self._state = replace(self._state, **changes)
        return self._emit()

    # --- identity -------------------------------------------------------

    def set_nickname(self, nickname: str) -> bool:
        """Store the display name; returns True when it actually changed."""
        if nickname == self._state.identity.nickname:
            return False
        self._set(identity=replace(self._state.identity, nickname=nickname))
        return True

    def set_ip(self, ip: str) -> None:
        if ip != self._state.identity.ip:
            self._set(identity=replace(self._state.identity, ip=ip))

    # --- peers ----------------------------------------------------------

    def set_scanning(self, scanning: bool) -> None:
        if scanning != self._state.scanning:
            self._set(scanning=scanning)

    def set_peers(self, peers: Sequence[PeerView]) -> None:
        """Replace the peer table.

        The selection is cleared only if the selected peer vanished, so a
        transient discovery hiccup cannot throw away the user's choice.
        """
        ordered = tuple(sorted(peers, key=lambda p: (p.name.lower(), p.host)))
        selected_id = self._state.selected_id
        if selected_id is not None and not any(p.id == selected_id for p in ordered):
            selected_id = None
        self._set(peers=ordered, selected_id=selected_id)

    def select(self, peer_id: str) -> Optional[PeerView]:
        """Toggle a peer: selecting the selected one clears the selection."""
        if self._state.selected_id == peer_id:
            self._set(selected_id=None)
            return None
        for peer in self._state.peers:
            if peer.id == peer_id:
                self._set(selected_id=peer_id)
                return peer
        return None

    def clear_selection(self) -> None:
        if self._state.selected_id is not None:
            self._set(selected_id=None)

    # --- layout ---------------------------------------------------------

    def slots(self, size: float) -> dict[str, tuple[float, float]]:
        """Radar seat for each known peer inside a ``size`` square field."""
        return peer_slots([p.id for p in self._state.peers], size)
