"""Controller repaint rules: the identity card must not be rebuilt under you.

The nickname field lives *inside* the identity card. Every rebuild of that
card swaps the field's parent subtree, the client recreates the input widget
and focus is lost mid-word — which is exactly what typing used to do.
"""

import time

import pytest

from app import config
from app.controller import ACTION_RESERVE, App
from app.models import (
    Batch,
    Direction,
    IncomingOffer,
    Item,
    PeerView,
    Phase,
    TransferRecord,
)


class FakePage:
    width = 420
    height = 840

    def __init__(self):
        self.controls = []
        self.updates = 0

    def add(self, *controls):
        self.controls.extend(controls)

    def clean(self):
        self.controls.clear()

    def update(self, *controls):
        self.updates += 1

    def schedule_update(self):
        self.updates += 1

    def run_task(self, fn, *args, **kwargs):
        return None

    def show_dialog(self, dialog):
        self.last_dialog = dialog

    def pop_dialog(self):
        pass


class Event:
    def __init__(self, control):
        self.control = control


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # never touch ~/.config
    page = FakePage()
    application = App(page)
    application._render_all()
    return application


def type_into(application, text: str) -> None:
    """Simulate typing the whole name, one keystroke at a time."""
    for i in range(1, len(text) + 1):
        application._name_field.value = text[:i]
        application._on_nickname_change(Event(application._name_field))


MAYA = PeerView(id="aaaa", name="Maya's Laptop", host="192.168.1.24", port=47554)


def one_file_batch() -> Batch:
    return Batch(items=(Item(path="a.txt", size=1),))


def finished_record(peer: PeerView) -> TransferRecord:
    return TransferRecord(
        direction=Direction.OUT,
        peer_name=peer.name,
        peer_host=peer.host,
        label="a.txt",
        item_count=1,
        total_bytes=1,
        started_at=0.0,
        finished_at=1.0,
    )


def test_typing_a_nickname_does_not_rebuild_the_card(app):
    card = app._identity_slot.content
    field = app._name_field

    type_into(app, "David")

    assert app._identity_slot.content is card  # same subtree -> focus survives
    assert app._name_field is field
    assert app.radar.identity.nickname == "David"
    assert app.cfg.nickname == "David"
    # ...and the keystroke was persisted
    assert config.load().nickname == "David"


def test_typing_still_repaints_the_radar(app):
    before = app._field_host.content
    type_into(app, "David")
    # the monogram on the radar tracks the nickname, so the field repaints
    assert app._field_host.content is not before
    assert app.page.updates > 0


def test_a_peer_refresh_leaves_the_card_alone(app):
    card = app._identity_slot.content
    app.radar.set_peers([PeerView(id="aaaa", name="Maya's Laptop", host="192.168.1.24", port=47554)])
    assert app._identity_slot.content is card
    assert app.radar.state.peer_count == 1


def test_an_empty_name_falls_back_instead_of_wiping_it(app):
    type_into(app, "David")
    app._name_field.value = ""
    app._on_nickname_change(Event(app._name_field))
    assert app.radar.identity.nickname == "David"


def test_the_card_is_rebuilt_when_it_really_changes(app):
    card = app._identity_slot.content

    # the IP pill swaps to "Copied to clipboard" -> the card *must* rebuild
    app._ip_copied_until = time.monotonic() + 5
    app._render_identity()
    assert app._identity_slot.content is not card

    copied_card = app._identity_slot.content
    app._ip_copied_until = 0.0
    app._render_identity()
    assert app._identity_slot.content is not copied_card


def test_a_new_ip_rebuilds_the_card(app):
    card = app._identity_slot.content
    app.radar.set_ip("10.0.0.7")
    assert app._identity_slot.content is not card


# --- the scanning layout --------------------------------------------------


def test_the_radar_keeps_one_sweep_across_repaints(app):
    """The control animates itself, so a repaint must not recreate it."""
    app._render_field()
    sweep = app._sweep
    assert sweep.size == app._field_size()

    app._render_field()  # ordinary repaint (peer list, nickname, ...)
    assert app._sweep is sweep  # same instance -> the loop is untouched

    app.page.height = 500  # a smaller window gives a smaller radar
    app._render_field()
    assert app._sweep is sweep  # re-fitted, not replaced
    assert sweep.size == app._field_size()
    assert sweep.size != 372.0  # ...and it really did change


def test_action_panel_is_hidden_until_a_peer_is_selected(app):
    hidden = app._action_slot.content
    assert hidden.height == 0  # no panel on screen while scanning
    assert ACTION_RESERVE[Phase.IDLE] == 0.0  # ...and no room reserved for it

    app.radar.set_peers(
        [PeerView(id="aaaa", name="Maya's Laptop", host="192.168.1.24", port=47554)]
    )
    app._select_peer("aaaa")
    shown = app._action_slot.content
    assert shown is not hidden
    assert shown.height is None  # a real card, not the placeholder

    app._select_peer("aaaa")  # tapping the selected peer deselects it
    assert app._action_slot.content.height == 0


def test_every_other_phase_still_shows_the_panel(app):
    """Only IDLE hides it — an incoming offer must always be answerable."""
    peer = MAYA
    app.radar.set_peers([peer])
    tm = app.transfer

    def shown(phase):
        app._render_action()
        assert app._action_slot.content.height is None, phase

    app._select_peer("aaaa")
    shown(Phase.SELECTED)

    tm.set_batch(one_file_batch())
    shown(Phase.PICKED)

    tm.ring()
    shown(Phase.RINGING)

    tm.begin_transfer(1)
    tm.advance(1)
    shown(Phase.TRANSFERRING)

    tm.finish(finished_record(peer))
    shown(Phase.DONE)

    tm.reset()
    tm.incoming(
        IncomingOffer(
            batch_id="b",
            sender_id="cccc",
            sender_name="studio-pc",
            sender_host="192.168.1.31",
            label="clip.mp4",
            item_count=1,
            file_count=1,
            total=999,
        )
    )
    shown(Phase.INCOMING)

    tm.decline("Rejected")
    shown(Phase.FAILED)

    tm.reset()
    tm.clear_peer()
    app._render_action()
    assert app._action_slot.content.height == 0  # back to scanning


def test_the_panel_hides_when_the_peer_leaves_the_network(app):
    """After a transfer, a peer that stops beaconing takes the panel with it."""
    app.radar.set_peers([MAYA])
    app._select_peer("aaaa")
    tm = app.transfer
    tm.set_batch(one_file_batch())
    tm.ring()
    tm.begin_transfer(1)
    tm.finish(finished_record(MAYA))
    assert tm.phase is Phase.DONE
    assert app._action_slot.content.height is None  # "Send again" is up

    app.radar.set_peers([])  # peer quit -> discovery pruned it (PEER_EXPIRY)
    assert app.radar.state.selected_id is None
    assert tm.phase is Phase.IDLE
    assert app._action_slot.content.height == 0  # ...and the panel is gone


def test_a_live_peer_keeps_the_panel_up(app):
    app.radar.set_peers([MAYA])
    app._select_peer("aaaa")
    app.transfer.set_batch(one_file_batch())

    app.radar.set_peers([MAYA])  # routine beacon refresh
    assert app.transfer.phase is Phase.PICKED
    assert app._action_slot.content.height is None


def test_a_transfer_in_flight_is_left_alone(app):
    """The transport decides when a call is dead — not the radar."""
    app.radar.set_peers([MAYA])
    app._select_peer("aaaa")
    tm = app.transfer
    tm.set_batch(one_file_batch())
    tm.ring()  # RINGING: busy

    app.radar.set_peers([])  # discovery gap while we wait for an answer
    assert tm.phase is Phase.RINGING
    assert app._action_slot.content.height is None


def test_an_offline_sender_still_gets_an_answer(app):
    """An offer from a peer that never shows up in our list stays answerable."""
    tm = app.transfer
    tm.incoming(
        IncomingOffer(
            batch_id="b",
            sender_id="cccc",
            sender_name="studio-pc",
            sender_host="192.168.1.31",
            label="clip.mp4",
            item_count=1,
            file_count=1,
            total=999,
        )
    )
    app.radar.set_peers([MAYA])  # sender itself is not in the peer table
    assert tm.phase is Phase.INCOMING
    assert app._action_slot.content.height is None
