"""Transfer state machine: pick → ring → transfer → done (both directions).

Mirrors :mod:`app.radar_vm` in spirit: immutable snapshots, guarded
transitions, no Flet and no lanlink. The controller drives the transitions;
the views only ever render the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Optional

from .models import (
    Batch,
    Direction,
    IncomingOffer,
    Phase,
    PeerView,
    TransferProgress,
    TransferRecord,
)


class IllegalTransition(Exception):
    """A transition that the current phase does not allow (a bug if raised)."""


@dataclass(frozen=True)
class TransferState:
    """Everything the action area needs to draw itself."""

    phase: Phase = Phase.IDLE
    peer: Optional[PeerView] = None
    batch: Optional[Batch] = None
    direction: Direction = Direction.OUT
    incoming: Optional[IncomingOffer] = None
    progress: Optional[TransferProgress] = None
    record: Optional[TransferRecord] = None
    error: str = ""

    @property
    def label(self) -> str:
        """Human name of the thing currently moving (or last moved)."""
        if self.incoming is not None:
            return self.incoming.label
        if self.batch is not None:
            return self.batch.label
        if self.record is not None:
            return self.record.label
        return ""

    @property
    def is_outgoing(self) -> bool:
        return self.direction is Direction.OUT


class TransferViewModel:
    def __init__(self, *, on_change: Optional[Callable[[TransferState], None]] = None) -> None:
        self._state = TransferState()
        self._on_change = on_change

    # --- state ----------------------------------------------------------

    @property
    def state(self) -> TransferState:
        return self._state

    @property
    def phase(self) -> Phase:
        return self._state.phase

    @property
    def busy(self) -> bool:
        return self._state.phase.busy

    def _emit(self) -> TransferState:
        if self._on_change is not None:
            self._on_change(self._state)
        return self._state

    def _set(self, **changes) -> TransferState:
        self._state = replace(self._state, **changes)
        return self._emit()

    def _require(self, allowed: set[Phase], action: str) -> None:
        if self._state.phase not in allowed:
            raise IllegalTransition(
                f"{action} not allowed in phase {self._state.phase.value}"
            )

    # --- selection (blocked while a transfer runs) ----------------------

    def select_peer(self, peer: PeerView) -> None:
        self._require({Phase.IDLE, Phase.SELECTED, Phase.PICKED, Phase.DONE, Phase.FAILED}, "select peer")
        self._set(
            phase=Phase.SELECTED,
            peer=peer,
            batch=None,
            incoming=None,
            progress=None,
            record=None,
            error="",
        )

    def clear_peer(self) -> None:
        self._require({Phase.IDLE, Phase.SELECTED, Phase.PICKED, Phase.DONE, Phase.FAILED}, "clear peer")
        self._set(
            phase=Phase.IDLE,
            peer=None,
            batch=None,
            incoming=None,
            progress=None,
            record=None,
            error="",
        )

    # --- picking --------------------------------------------------------

    def set_batch(self, batch: Batch) -> None:
        self._require({Phase.SELECTED, Phase.PICKED}, "pick files")
        self._set(phase=Phase.PICKED, batch=batch, record=None, error="")

    def clear_batch(self) -> None:
        self._require({Phase.SELECTED, Phase.PICKED}, "discard selection")
        self._set(phase=Phase.SELECTED, batch=None)

    # --- outgoing flow ---------------------------------------------------

    def ring(self) -> None:
        """Handshake sent; waiting for the other side to answer.

        Also re-arms ``direction``: accepting an incoming offer leaves it at
        IN, and nothing else on the outgoing path resets it, so a later send
        would be drawn as "Receiving from …".
        """
        self._require({Phase.PICKED}, "start outgoing transfer")
        self._set(
            phase=Phase.RINGING,
            direction=Direction.OUT,
            record=None,
            error="",
            progress=None,
        )

    # --- incoming flow ---------------------------------------------------

    def incoming(self, offer: IncomingOffer) -> None:
        if self.busy:
            raise IllegalTransition(f"offer arrived during {self._state.phase.value}")
        self._set(
            phase=Phase.INCOMING,
            direction=Direction.IN,
            incoming=offer,
            peer=PeerView(
                id=offer.sender_id,
                name=offer.sender_name,
                host=offer.sender_host,
                port=0,
            ),
            batch=None,
            progress=None,
            record=None,
            error="",
        )

    def decline(self, reason: str) -> None:
        """Local reject: nothing was transferred, but say so out loud."""
        self._require({Phase.INCOMING}, "decline offer")
        self._set(phase=Phase.FAILED, incoming=None, error=reason)

    # --- payload ---------------------------------------------------------

    def begin_transfer(self, total: int) -> None:
        """Both sides call this once the call is answered and consented."""
        self._require({Phase.RINGING, Phase.INCOMING}, "begin transfer")
        self._set(phase=Phase.TRANSFERRING, progress=TransferProgress.start(total))

    def advance(self, done: int, *, current_path: str = "") -> None:
        """Called from progress callbacks — may fire many times a second."""
        if self._state.phase is not Phase.TRANSFERRING or self._state.progress is None:
            return
        total = self._state.progress.total
        clamped = max(0, min(done, total))
        self._set(progress=self._state.progress.advanced(clamped, current_path=current_path))

    def finish(self, record: TransferRecord) -> None:
        self._require({Phase.TRANSFERRING, Phase.RINGING, Phase.INCOMING}, "finish transfer")
        self._set(
            phase=Phase.DONE,
            record=record,
            progress=None,
            incoming=None,
            batch=None,
        )

    def fail(self, error: str, record: Optional[TransferRecord] = None) -> None:
        if self._state.phase in (Phase.IDLE, Phase.SELECTED) and record is None:
            raise IllegalTransition(f"nothing to fail in phase {self._state.phase.value}")
        self._set(phase=Phase.FAILED, error=error, record=record, progress=None, incoming=None)

    # --- after everything -------------------------------------------------

    def reset(self) -> None:
        """Ready for the next transfer — keeps the peer, drops the debris."""
        self._require({Phase.DONE, Phase.FAILED}, "reset")
        phase = Phase.SELECTED if self._state.peer is not None else Phase.IDLE
        self._set(
            phase=phase,
            batch=None,
            incoming=None,
            progress=None,
            record=None,
            error="",
        )
