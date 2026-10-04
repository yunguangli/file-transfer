"""State machines: radar selection/seating and the transfer lifecycle."""

import math

import pytest

from app.models import (
    Batch,
    Direction,
    Identity,
    IncomingOffer,
    Item,
    Phase,
    PeerView,
    TransferRecord,
)
from app.radar_vm import (
    OVERFLOW_RADIUS_STEP,
    RING_RADII,
    RadarViewModel,
    peer_slots,
)
from app.transfer_vm import IllegalTransition, TransferViewModel

IDENT = Identity(peer_id="self", nickname="me", hostname="lyg", ip="192.168.1.113")


def peers(*names: str) -> list[PeerView]:
    return [
        PeerView(id=f"id-{n}", name=n, host=f"192.168.1.{i}", port=47554)
        for i, n in enumerate(names, start=1)
    ]


# --- radar ----------------------------------------------------------------


def test_state_starts_scanning_with_no_peers():
    vm = RadarViewModel(IDENT)
    state = vm.state
    assert state.peers == ()
    assert state.selected_id is None
    assert state.scanning is True
    assert state.title == "Scan in Progress"
    assert "Looking for devices" in state.status_line


def test_title_follows_selection():
    vm = RadarViewModel(IDENT)
    vm.set_peers(peers("Maya's Laptop"))
    assert vm.state.title == "Choose a Peer"
    assert vm.state.status_line == "1 nearby peer"

    vm.select("id-Maya's Laptop")
    assert vm.state.title == "Peer Selected"
    assert vm.state.status_line == "Connected to Maya's Laptop"


def test_selecting_the_selected_peer_toggles_it_off():
    vm = RadarViewModel(IDENT)
    vm.set_peers(peers("a"))
    assert vm.select("id-a") is not None
    assert vm.state.selected_id == "id-a"
    assert vm.select("id-a") is None
    assert vm.state.selected_id is None


def test_selecting_an_unknown_peer_is_a_no_op():
    vm = RadarViewModel(IDENT)
    assert vm.select("ghost") is None
    assert vm.state.selected_id is None


def test_peers_are_sorted_and_selection_survives_a_refresh():
    vm = RadarViewModel(IDENT)
    vm.set_peers(peers("zeta", "alpha"))
    assert [p.name for p in vm.state.peers] == ["alpha", "zeta"]

    vm.select("id-zeta")
    vm.set_peers(peers("zeta", "alpha"))  # discovery hiccup, same table
    assert vm.state.selected_id == "id-zeta"

    vm.set_peers(peers("alpha"))  # the selected peer vanished
    assert vm.state.selected_id is None
    assert vm.state.selected is None


def test_set_nickname_reports_whether_it_changed():
    vm = RadarViewModel(IDENT)
    assert vm.set_nickname("lyg") is True
    assert vm.set_nickname("lyg") is False
    assert vm.identity.nickname == "lyg"


def test_on_change_fires_after_every_mutation():
    seen = []
    vm = RadarViewModel(IDENT, on_change=seen.append)
    vm.set_scanning(False)
    vm.set_peers(peers("a"))
    vm.select("id-a")
    assert len(seen) == 3
    assert seen[-1] is vm.state  # the listener always gets the live snapshot


# --- radar seating --------------------------------------------------------


def test_seats_are_deterministic_and_sit_on_a_ring():
    ids = [f"peer-{i}" for i in range(6)]
    first = peer_slots(ids, 300.0)
    second = peer_slots(list(reversed(ids)), 300.0)

    assert first == second  # order in, order out: same seats
    for x, y in first.values():
        radius = math.hypot(x - 150.0, y - 150.0) / 300.0
        # a seat sits on one of the fixed rings, pushed out by whole
        # overflow steps only when two peers hashed onto the same one
        assert any(
            abs(radius - ring - OVERFLOW_RADIUS_STEP * steps) < 1e-9
            for ring in RING_RADII
            for steps in range(6)
        ), radius


def test_every_peer_gets_its_own_seat_up_to_the_seat_count():
    ids = [f"peer-{i}" for i in range(12)]  # exactly the fixed seat count
    seats = peer_slots(ids, 300.0)
    assert len(set(seats.values())) == len(ids)


def test_a_peer_list_bigger_than_the_seats_never_hangs():
    """Regression: once all twelve seats were taken, this spun forever —
    discovery of a busy LAN would have frozen the whole app."""
    ids = [f"peer-{i}" for i in range(40)]
    seats = peer_slots(ids, 300.0)

    assert set(seats) == set(ids)  # everyone still gets a position
    for x, y in seats.values():
        assert math.hypot(x - 150.0, y - 150.0) <= 0.46 * 300.0 + 1e-9  # on the disc


# --- transfer lifecycle ---------------------------------------------------


def make_batch() -> Batch:
    return Batch(items=(Item(path="Photos/a.jpg", size=10),))


def record() -> TransferRecord:
    return TransferRecord(
        direction=Direction.OUT,
        peer_name="Maya's Laptop",
        peer_host="192.168.1.24",
        label="Photos",
        item_count=1,
        total_bytes=10,
        started_at=0.0,
        finished_at=1.0,
    )


def offer() -> IncomingOffer:
    return IncomingOffer(
        batch_id="b",
        sender_id="cccc",
        sender_name="studio-pc",
        sender_host="192.168.1.31",
        label="clip.mp4",
        item_count=1,
        file_count=1,
        total=999,
    )


def test_outgoing_happy_path_leaves_us_ready_for_the_next_one():
    vm = TransferViewModel()
    vm.select_peer(peers("Maya's Laptop")[0])
    vm.set_batch(make_batch())
    vm.ring()
    assert vm.phase is Phase.RINGING and vm.busy

    vm.begin_transfer(10)
    vm.advance(4, current_path="Photos/a.jpg")
    assert vm.state.progress.done == 4

    vm.finish(record())
    assert vm.phase is Phase.DONE
    assert vm.state.label == "Photos"

    vm.reset()
    # the peer stays selected, everything else is cleared
    assert vm.phase is Phase.SELECTED
    assert vm.state.batch is None and vm.state.record is None and vm.state.error == ""


def test_progress_is_clamped_and_ignored_outside_a_transfer():
    vm = TransferViewModel()
    vm.select_peer(peers("a")[0])
    vm.set_batch(make_batch())
    vm.ring()
    vm.begin_transfer(10)

    vm.advance(99)
    assert vm.state.progress.done == 10  # clamped to the total
    vm.advance(-5)
    assert vm.state.progress.done == 0

    vm.finish(record())
    vm.advance(3)  # not transferring any more: silent no-op
    assert vm.state.progress is None


def test_selection_is_blocked_while_a_transfer_runs():
    vm = TransferViewModel()
    vm.select_peer(peers("a")[0])
    vm.set_batch(make_batch())
    vm.ring()

    with pytest.raises(IllegalTransition):
        vm.select_peer(peers("b")[0])
    with pytest.raises(IllegalTransition):
        vm.clear_peer()
    with pytest.raises(IllegalTransition):
        vm.set_batch(make_batch())


def test_picking_requires_a_selected_peer():
    vm = TransferViewModel()
    with pytest.raises(IllegalTransition):
        vm.set_batch(make_batch())


def test_incoming_flow_sets_direction_and_peer():
    vm = TransferViewModel()
    vm.incoming(offer())
    assert vm.phase is Phase.INCOMING
    assert vm.state.direction is Direction.IN
    assert vm.state.peer.name == "studio-pc"
    assert vm.state.label == "clip.mp4"

    vm.begin_transfer(999)
    vm.advance(500)
    vm.finish(
        TransferRecord(
            direction=Direction.IN,
            peer_name="studio-pc",
            peer_host="192.168.1.31",
            label="clip.mp4",
            item_count=1,
            total_bytes=999,
            started_at=0.0,
            finished_at=2.0,
        )
    )
    assert vm.phase is Phase.DONE
    vm.reset()
    # the sender is kept as the current peer, so replying is one tap away
    assert vm.phase is Phase.SELECTED
    assert vm.state.peer.name == "studio-pc"
    assert vm.state.record is None and vm.state.error == ""


def test_an_offer_during_a_busy_phase_is_refused():
    vm = TransferViewModel()
    vm.select_peer(peers("a")[0])
    vm.set_batch(make_batch())
    vm.ring()
    with pytest.raises(IllegalTransition):
        vm.incoming(offer())


def test_declining_an_offer_reports_a_failure_with_a_reason():
    vm = TransferViewModel()
    vm.incoming(offer())
    vm.decline("Rejected by Maya's Laptop")
    assert vm.phase is Phase.FAILED
    assert vm.state.error == "Rejected by Maya's Laptop"
    assert vm.state.incoming is None


def test_failing_with_nothing_in_flight_is_a_bug():
    vm = TransferViewModel()
    with pytest.raises(IllegalTransition):
        vm.fail("connection lost")


def test_reset_after_a_failure_is_allowed():
    vm = TransferViewModel()
    vm.select_peer(peers("a")[0])
    vm.set_batch(make_batch())
    vm.ring()
    vm.fail("The connection was lost mid-transfer")
    assert vm.phase is Phase.FAILED
    vm.reset()
    assert vm.phase is Phase.SELECTED
