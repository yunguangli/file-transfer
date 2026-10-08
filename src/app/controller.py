"""Flet + lanlink wiring: the one module that imports both worlds.

Layers stay honest here: views render state, viewmodels own state, lanlink
owns bytes on the wire. This module only moves state between them.

Flows worth knowing:

* **outgoing** — pick → ``ring()`` → :func:`lanlink.dial` (offer is the
  handshake) → ``begin_transfer()`` → stream files → final reply → ``finish()``.
* **incoming** — lanlink ``validate`` parses the offer, ``on_ringing`` opens
  the consent dialog, ``accept`` awaits the user's tap, ``on_incoming``
  writes the payload to ``~/Downloads/LanLink/<peer>_<stamp>/``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

import flet as ft

from lanlink import Discovery, SessionServer, clean_name, dial
from lanlink.errors import (
    CallRejected,
    HandshakeError,
    LineBusy,
    PeerGone,
    TransferAborted,
)

from . import config, ipaddr, protocol, theme as T
from .models import (
    Batch,
    Direction,
    Identity,
    IncomingOffer,
    PeerView,
    Phase,
    TransferRecord,
)
from .radar_vm import RadarViewModel
from .transfer_vm import IllegalTransition, TransferState, TransferViewModel
from .views import action_card, dialogs, radar_view
from .views.action_card import ActionHandlers
from .views.radar_sweep import RadarSweep

_log = logging.getLogger(__name__)

FINAL_REPLY_TIMEOUT = 30.0  # receiver renders its finished page before replying
SEND_CHUNK = 256 * 1024  # progress repaint granularity
PROGRESS_TICK = 0.2  # seconds between progress repaints
RECEIPT_START_TIMEOUT = 4.0  # consent answered but payload never began

# Flet has no layout measurement, so the radar reserves a known amount of
# room per action-card phase instead of guessing. IDLE reserves nothing:
# the panel is hidden until a peer is picked, so the radar takes the space.
ACTION_RESERVE = {
    Phase.IDLE: 0.0,
    Phase.SELECTED: 205.0,
    Phase.PICKED: 235.0,
    Phase.RINGING: 215.0,
    Phase.INCOMING: 150.0,
    Phase.TRANSFERRING: 480.0,
    Phase.DONE: 310.0,
    Phase.FAILED: 300.0,
}
TOPBAR_RESERVE = 64.0
IDENTITY_RESERVE = 186.0
SIDEBAR_WIDTH = 380.0
APP_WIDTH = 1040.0


class _Cancelled(Exception):
    """User pressed cancel mid-transfer."""


class _CancelledReceive(Exception):
    """Receiver pressed stop mid-transfer."""


def _friendly(exc: BaseException) -> str:
    """Turn a transport exception into a sentence a person can act on."""
    if isinstance(exc, TransferAborted):
        return "The peer hung up before the transfer finished"
    if isinstance(exc, (ConnectionError, OSError)):
        return "The connection was lost"
    if isinstance(exc, protocol.ProtocolError):
        return str(exc)
    return f"Transfer failed ({type(exc).__name__}: {exc})"


class App:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        first_run = not config.default_path().exists()
        self.cfg = config.load()
        hostname = ipaddr.hostname()
        if first_run:
            # First launch: greet with the machine's name instead of "unnamed".
            self.cfg.nickname = clean_name(hostname, fallback=self.cfg.nickname)
            config.save(self.cfg)
        self.radar = RadarViewModel(
            Identity(
                peer_id=self.cfg.peer_id,
                nickname=self.cfg.nickname,
                hostname=hostname,
                ip=ipaddr.local_ip(),
            ),
            on_change=self._on_radar_change,
        )
        self.transfer = TransferViewModel(on_change=self._on_transfer_change)
        self.records: list[TransferRecord] = []
        self.dest_base = Path.home() / "Downloads" / "LanLink"

        self._handlers = ActionHandlers(
            on_pick_files=lambda: self.page.run_task(self._open_file_sheet),
            on_clear_peer=self._clear_peer_clicked,
            on_transfer=lambda: self.page.run_task(self._start_outgoing),
            on_change_selection=lambda: self.page.run_task(self._open_file_sheet),
            on_cancel=lambda: self.page.run_task(self._cancel_active),
            on_reset=self._reset_clicked,
        )

        # Persistent controls — rebuilt roots re-parent these, never recreate
        # them (the nickname field would lose focus every repaint).
        self._topbar_slot = ft.Container()
        self._field_host = ft.Container(
            expand=True,
            alignment=ft.Alignment.CENTER,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        self._identity_slot = ft.Container()
        self._identity_key: tuple = ()  # what the identity card is built from
        self._action_slot = ft.Container()
        self._name_field = self._build_name_field()
        # One sweep instance for the life of the page: it animates itself
        # (did_mount/will_unmount) and is only re-fitted when the radar
        # changes size, so repainting the field neither restarts nor
        # duplicates the beam.
        self._sweep: Optional[RadarSweep] = None

        self._file_picker: Optional[ft.FilePicker] = None
        self._clipboard: Optional[ft.Clipboard] = None

        self._server: Optional[SessionServer] = None
        self._discovery: Optional[Discovery] = None
        self._pending_offer: Optional[protocol.Offer] = None
        self._accept_future: Optional[asyncio.Future] = None
        self._active_conn = None
        self._cancel_requested = False
        self._receiving_started = False
        self._open_dialog: Optional[ft.DialogControl] = None

        self._painted_phase: Optional[Phase] = None
        self._last_action_paint = 0.0
        self._progress_flush_pending = False
        self._layout_key: tuple = ()
        self._ip_copied_until = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        page = self.page
        page.title = "myftp"
        page.bgcolor = T.BG
        page.padding = 0
        page.theme_mode = ft.ThemeMode.LIGHT
        page.on_resize = self._on_resize

        # Constructed here (not in __init__) so Flet's service auto-
        # registration finds an active page context.
        self._file_picker = ft.FilePicker()
        self._clipboard = ft.Clipboard()

        self._render_topbar()
        self._render_field()
        self._render_identity()
        self._render_action()
        page.add(self._build_root())
        page.update()

        try:
            await self._start_transport()
        except Exception:  # noqa: BLE001 - a dead network must not kill the UI
            _log.exception("transport failed to start")
            self._fail_inplace("Network unavailable — peers cannot be discovered")

    async def _start_transport(self) -> None:
        self._server = SessionServer(
            is_busy=lambda: self.transfer.busy,
            on_ringing=self._on_ringing,
            accept=self._accept,
            validate=self._validate_offer,
            on_incoming=self._on_incoming,
            on_error=self._on_session_error,
            accept_delay=0.0,
        )
        port = await self._server.start()
        self._discovery = Discovery(
            name=self.radar.identity.nickname,
            tcp_port=port,
            on_change=self._on_peers,
        )
        await self._discovery.start()

    # The radar beam animates itself: `RadarSweep` (views/radar_sweep.py)
    # starts its own task in `did_mount()` and cancels it in
    # `will_unmount()`, so the controller only ever creates and re-fits it.

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------

    def _is_compact(self) -> bool:
        return float(self.page.width or 420) < T.COMPACT_BREAKPOINT

    def _build_root(self) -> ft.Control:
        compact = self._is_compact()
        padding = ft.Padding.only(left=16, right=16, top=14, bottom=16)
        if compact:
            body: ft.Control = ft.Column(
                [
                    self._topbar_slot,
                    self._field_host,
                    self._identity_slot,
                    self._action_slot,
                ],
                spacing=14,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
            width = None
        else:
            left = ft.Column(
                [self._topbar_slot, self._field_host, self._identity_slot],
                spacing=14,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            )
            right = ft.Column(
                [self._action_slot],
                width=SIDEBAR_WIDTH,
                spacing=0,
                expand=True,
                scroll=ft.ScrollMode.AUTO,
            )
            body = ft.Row(
                [left, right],
                spacing=26,
                expand=True,
                alignment=ft.MainAxisAlignment.CENTER,
            )
            width = APP_WIDTH
        return ft.Container(
            expand=True,
            bgcolor=T.BG,
            alignment=ft.Alignment.CENTER,
            content=ft.Container(
                width=width,
                padding=padding,
                content=body,
            ),
        )

    def _field_size(self) -> float:
        """Side of the square radar field for the current window + phase."""
        width = float(self.page.width or 420)
        height = float(self.page.height or 780)
        gaps = 14.0 * 4
        if self._is_compact():
            action = ACTION_RESERVE.get(self.transfer.phase, 240.0)
            reserve = TOPBAR_RESERVE + IDENTITY_RESERVE + action + gaps + 30.0
            avail_w = width - 48.0
        else:
            reserve = TOPBAR_RESERVE + IDENTITY_RESERVE + gaps + 30.0
            avail_w = min(APP_WIDTH, width - 48.0) - SIDEBAR_WIDTH - 26.0
        return max(200.0, min(avail_w, height - reserve))

    def _on_resize(self, e: ft.PageResizeEvent) -> None:
        key = (
            float(e.width) < T.COMPACT_BREAKPOINT,
            int(e.width // 60),
            int(e.height // 60),
        )
        if key != self._layout_key:
            self._layout_key = key
            self.page.clean()
            self.page.add(self._build_root())
        self._render_field()
        self.page.update()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------

    def _render_topbar(self) -> None:
        self._topbar_slot.content = radar_view.build_topbar(
            self.radar.state,
            on_back=self._back_action(),
            on_bell=self._open_history,
        )

    def _render_field(self) -> None:
        side = self._field_size()
        if self._sweep is None:
            self._sweep = RadarSweep(size=side)
        else:
            self._sweep.resize(side)  # same instance: angle and task survive
        self._field_host.content = radar_view.build_field(
            self.radar.state,
            size=side,
            sweep=self._sweep,
            on_select=self._select_peer,
        )

    def _render_identity(self) -> None:
        copied = time.monotonic() < self._ip_copied_until
        identity = self.radar.state.identity
        # Rebuild only when something *inside* the card changed. Swapping the
        # card swaps the text field's parent subtree, so the client recreates
        # the input widget and typing would drop focus after every keystroke
        # (the nickname itself is displayed by the field, not by the card).
        key = (identity.hostname, identity.ip, copied)
        if self._identity_slot.content is not None and key == self._identity_key:
            return
        self._identity_key = key
        self._identity_slot.content = radar_view.build_identity_card(
            self.radar.state,
            name_field=self._name_field,
            on_copy_ip=lambda: self.page.run_task(self._copy_ip),
            on_settings=self._open_settings,
            on_peer_list=self._open_peer_list,
            copied=copied,
        )

    def _render_action(self) -> None:
        state = self.transfer.state
        # Nothing to act on until a peer is picked: while the radar is still
        # scanning, the panel stays out of the way. A zero-height placeholder
        # keeps the slot itself in the tree, so no root rebuild is needed when
        # the card comes back on the next phase change.
        if state.phase is Phase.IDLE:
            self._action_slot.content = ft.Container(height=0)
            self._painted_phase = state.phase
            return
        identity = self.radar.identity
        self._action_slot.content = action_card.build_action_card(
            state,
            self._handlers,
            own=(identity.peer_id, identity.nickname),
        )
        self._painted_phase = state.phase

    def _render_all(self) -> None:
        self._render_topbar()
        self._render_field()
        self._render_identity()
        self._render_action()
        self.page.update()

    # --- change sinks -------------------------------------------------

    def _on_radar_change(self, state) -> None:
        self._drop_panel_if_peer_left(state)
        self._render_topbar()
        self._render_field()
        self._render_identity()
        self._render_action()
        self.page.update()

    def _drop_panel_if_peer_left(self, state) -> None:
        """Hide the action panel once the peer it points at has left.

        Discovery forgets a device after ``PEER_EXPIRY`` seconds of silence
        and ``set_peers`` has already dropped the radar selection at that
        point — without this, the panel would keep offering "Send again" for
        a machine that is no longer on the network.

        Transfers in flight are left alone: the session reports its own
        failure when the socket actually breaks, and an incoming offer has to
        stay answerable even if its sender has stopped beaconing.
        """
        peer = self.transfer.state.peer
        if peer is None or self.transfer.busy:
            return
        if any(p.id == peer.id for p in state.peers):
            return
        # Non-busy phases are exactly what `clear_peer()` accepts, so this
        # cannot raise — the panel goes back to its hidden IDLE placeholder.
        self.transfer.clear_peer()

    def _on_transfer_change(self, state: TransferState) -> None:
        phase_changed = state.phase is not self._painted_phase
        if phase_changed:
            self._render_field()  # radar reserves room per phase
            self._render_topbar()  # back affordance depends on phase
        if (
            state.phase is Phase.TRANSFERRING
            and not phase_changed
            and not self._progress_flush_pending
        ):
            now = time.monotonic()
            wait = PROGRESS_TICK - (now - self._last_action_paint)
            if wait > 0:
                self._progress_flush_pending = True
                self.page.run_task(self._flush_progress, wait)
                return
        self._paint_action()

    async def _flush_progress(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._progress_flush_pending = False
        if self.transfer.phase is Phase.TRANSFERRING:
            self._paint_action()

    def _paint_action(self) -> None:
        self._last_action_paint = time.monotonic()
        self._render_action()
        # Always a real update: the deferred `schedule_update()` path only
        # runs in Flet's components mode, which this `page.add()` app never
        # enters — queued changes would sit there until something else
        # happened to repaint the page.
        self.page.update()

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    def _build_name_field(self) -> ft.TextField:
        return ft.TextField(
            value=self.cfg.nickname,
            label="Change name here",
            label_style=ft.TextStyle(
                size=13,
                weight=ft.FontWeight.W_700,
                color=T.MUTED,
                height=1.5,
            ),
            align_label_with_hint=True,
            hint_text="Your nickname",
            text_align=ft.TextAlign.CENTER,
            text_style=ft.TextStyle(
                size=24,
                weight=ft.FontWeight.W_800,
                color=T.INK,
            ),
            hint_style=ft.TextStyle(
                size=22,
                weight=ft.FontWeight.W_700,
                color=T.MUTED,
            ),
            text_size=24,
            color=T.INK,
            border=ft.NoInputBorder(),
            dense=True,
            width=300,
            tooltip="Peers on the network see this name",
            on_change=self._on_nickname_change,
            on_blur=self._on_nickname_blur,
        )

    def _on_nickname_change(self, e: ft.ControlEvent) -> None:
        raw = e.control.value
        effective = clean_name(raw, fallback=self.radar.identity.nickname)
        if not self.radar.set_nickname(effective):
            return  # nothing changed, and no repaint to schedule
        # `set_nickname` already notified `_on_radar_change`, which repaints
        # topbar/field/action — none of them is the card holding this input,
        # so the field keeps its focus and its cursor.
        self.cfg.nickname = effective
        config.save(self.cfg)
        if self._discovery is not None:
            self._discovery.set_name(effective)

    def _on_nickname_blur(self, e: ft.ControlEvent) -> None:
        """Normalize what the user typed back to the effective name."""
        if self._name_field.value.strip() != self.radar.identity.nickname:
            self._name_field.value = self.radar.identity.nickname
            self.page.update()

    async def _copy_ip(self) -> None:
        if self._clipboard is None:
            return
        await self._clipboard.set(self.radar.identity.ip)
        self._ip_copied_until = time.monotonic() + 1.6
        self._render_identity()
        self.page.update()
        await asyncio.sleep(1.6)
        self._ip_copied_until = 0.0
        self._render_identity()
        self.page.update()

    # ------------------------------------------------------------------
    # peers / selection
    # ------------------------------------------------------------------

    def _on_peers(self, peers) -> None:
        self.radar.set_peers(
            [PeerView(id=p.id, name=p.name, host=p.host, port=p.port) for p in peers]
        )

    def _select_peer(self, peer_id: str) -> None:
        if self.transfer.busy:
            return  # one call at a time; selection is frozen mid-transfer
        peer = self.radar.select(peer_id)
        if peer is None:
            self.transfer.clear_peer()
        else:
            self.transfer.select_peer(peer)
        self._render_topbar()
        self._render_field()
        self._render_action()
        self.page.update()

    def _clear_peer_clicked(self) -> None:
        if self.transfer.busy:
            return
        self.radar.clear_selection()
        self.transfer.clear_peer()
        self._render_all()

    def _back_action(self):
        if self.transfer.busy:
            return None
        if self.transfer.phase in (Phase.IDLE,):
            return None
        return self._back_clicked

    def _back_clicked(self) -> None:
        phase = self.transfer.phase
        if phase in (Phase.DONE, Phase.FAILED):
            self.transfer.reset()
        elif phase is Phase.PICKED:
            self.transfer.clear_batch()
        elif phase is Phase.SELECTED:
            self.radar.clear_selection()
            self.transfer.clear_peer()
        self._render_all()

    def _reset_clicked(self) -> None:
        try:
            self.transfer.reset()
        except IllegalTransition:
            pass
        self._render_all()

    # ------------------------------------------------------------------
    # dialogs & sheets
    # ------------------------------------------------------------------

    def _show_dialog(self, dialog: ft.DialogControl) -> None:
        if self._open_dialog is not None:
            self.page.pop_dialog()
        self._open_dialog = dialog
        self.page.show_dialog(dialog)

    def _close_dialog(self) -> None:
        if self._open_dialog is not None:
            self.page.pop_dialog()
            self._open_dialog = None

    def _open_history(self) -> None:
        self._show_dialog(
            dialogs.history_dialog(self.records, on_close=self._close_dialog)
        )

    def _open_settings(self) -> None:
        self._show_dialog(
            dialogs.settings_dialog(
                peer_id=self.radar.identity.peer_id,
                nickname=self.radar.identity.nickname,
                destination=str(self.dest_base),
                recent_count=len(self.cfg.recent),
                on_clear_recents=self._clear_recents,
                on_close=self._close_dialog,
            )
        )

    def _clear_recents(self) -> None:
        self.cfg.recent = []
        config.save(self.cfg)
        self._close_dialog()

    def _open_peer_list(self) -> None:
        self._show_dialog(
            dialogs.build_peer_list_sheet(
                self.radar.state.peers,
                self.radar.state.selected_id,
                on_select=self._select_peer_from_list,
                on_close=self._close_dialog,
            )
        )

    def _select_peer_from_list(self, peer_id: str) -> None:
        self._close_dialog()
        self._select_peer(peer_id)

    async def _open_file_sheet(self) -> None:
        if self.transfer.busy:
            return
        self._show_dialog(
            dialogs.build_file_sheet(
                self.cfg.recent,
                on_files=lambda: self.page.run_task(self._pick_files),
                on_folder=lambda: self.page.run_task(self._pick_folder),
                on_recent=lambda path: self.page.run_task(
                    self._prepare_batch, [path]
                ),
                on_close=self._close_dialog,
            )
        )

    # ------------------------------------------------------------------
    # picking
    # ------------------------------------------------------------------

    async def _pick_files(self) -> None:
        if self._file_picker is None or self.transfer.busy:
            return
        self._close_dialog()
        try:
            files = await self._file_picker.pick_files(allow_multiple=True)
        except Exception as ex:  # noqa: BLE001 - platform refused the dialog
            self._fail_inplace(f"Could not open the file picker ({ex})")
            return
        if not files:
            return  # user cancelled
        paths = [f.path for f in files if f.path]
        if not paths:
            self._fail_inplace("This platform does not expose file paths (web mode)")
            return
        await self._prepare_batch(paths)

    async def _pick_folder(self) -> None:
        if self._file_picker is None or self.transfer.busy:
            return
        self._close_dialog()
        try:
            path = await self._file_picker.get_directory_path()
        except Exception as ex:  # noqa: BLE001
            self._fail_inplace(f"Could not open the folder picker ({ex})")
            return
        if path:
            await self._prepare_batch([path])

    async def _prepare_batch(self, paths: list) -> None:
        self._close_dialog()
        if self.radar.selected is None:
            self._fail_inplace("Select a peer first")
            return
        try:
            items = protocol.collect_items([str(p) for p in paths])
        except (protocol.ProtocolError, OSError, ValueError) as ex:
            self._fail_inplace(str(ex))
            return
        config.push_recent(self.cfg, [str(p) for p in paths])
        config.save(self.cfg)
        try:
            self.transfer.set_batch(Batch(items=items))
        except IllegalTransition as ex:
            _log.warning("batch rejected: %s", ex)
            return
        self._render_topbar()
        self._render_field()
        self._render_action()
        self.page.update()

    def _fail_inplace(self, message: str) -> None:
        """Surface an error without wrecking the user's place in the flow."""
        try:
            self.transfer.fail(message)
        except IllegalTransition:
            _log.info("suppressed state error: %s", message)
        self._render_topbar()
        self._render_action()
        self.page.update()

    # ------------------------------------------------------------------
    # outgoing transfer
    # ------------------------------------------------------------------

    async def _start_outgoing(self) -> None:
        state = self.transfer.state
        peer, batch = state.peer, state.batch
        if state.phase is not Phase.PICKED or peer is None or batch is None:
            return

        self._cancel_requested = False
        self.transfer.ring()
        self._render_field()
        self._render_topbar()
        self._paint_action()

        identity = self.radar.identity
        offer = protocol.make_offer(
            batch,
            sender_id=identity.peer_id,
            sender_name=identity.nickname,
            sender_ip=identity.ip,
        )
        started = time.time()
        try:
            conn = await dial(
                peer.host,
                peer.port,
                offer,
                reply_timeout=protocol.ANSWER_TIMEOUT,
                on_connected=self._remember_conn,
            )
        except CallRejected:
            self._fail_inplace("They declined the transfer")
            return
        except LineBusy:
            self._fail_inplace("They are busy with another transfer")
            return
        except PeerGone:
            self._fail_inplace(f"Could not reach {peer.host}")
            return
        except TransferAborted:
            # A closed socket means either the peer hung up or we did; only
            # the first is worth reporting, the second is already on screen.
            if self._cancel_requested:
                self._active_conn = None
                return
            self._fail_inplace("They did not answer in time")
            return
        except Exception as ex:  # noqa: BLE001 - anything the network threw
            if self._cancel_requested:
                self._active_conn = None
                return
            _log.exception("dial failed")
            self._fail_inplace(f"Call failed ({ex})")
            return

        if self._cancel_requested:
            self._active_conn = None
            await conn.close()
            return

        self._active_conn = conn
        try:
            await self._stream_out(conn, batch, peer, started)
        finally:
            self._active_conn = None
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - already gone
                pass

    async def _stream_out(self, conn, batch: Batch, peer: PeerView, started: float) -> None:
        total = batch.total_bytes
        self.transfer.begin_transfer(total)
        self._paint_action()
        sent = 0
        try:
            for item in batch.items:
                if item.is_dir:
                    continue
                with open(item.source, "rb") as handle:
                    while True:
                        if self._cancel_requested:
                            raise _Cancelled()
                        chunk = handle.read(SEND_CHUNK)
                        if not chunk:
                            break
                        await conn.send_payload(chunk)
                        sent += len(chunk)
                        self.transfer.advance(sent, current_path=item.path)
            reply = await conn.recv_json(timeout=FINAL_REPLY_TIMEOUT)
        except _Cancelled:
            self._fail_inplace("You stopped the transfer")
            return
        except (OSError, TransferAborted) as ex:
            if self._cancel_requested:
                self._fail_inplace("You stopped the transfer")
            else:
                _log.warning("send aborted: %s", ex)
                self._fail_inplace("The connection was lost mid-transfer")
            return
        except Exception as ex:  # noqa: BLE001
            _log.exception("send failed")
            self._fail_inplace(f"Send failed ({ex})")
            return

        if self._cancel_requested:
            self._fail_inplace("You stopped the transfer")
            return
        if reply.get("reply") != "ok" or reply.get("bytes") != total:
            self._fail_inplace("The receiver reported a problem with the payload")
            return

        record = TransferRecord(
            direction=Direction.OUT,
            peer_name=peer.name,
            peer_host=peer.host,
            label=batch.label,
            item_count=batch.file_count,
            total_bytes=total,
            started_at=started,
            finished_at=time.time(),
            ok=True,
        )
        self.records.insert(0, record)
        self.transfer.finish(record)
        self._render_topbar()
        self._render_field()
        self._paint_action()

    def _remember_conn(self, conn) -> None:
        """Publish the live socket so "cancel" can hang up on a ringing call.

        ``dial`` only hands its connection back once the peer has answered, so
        during RINGING there is otherwise nothing for :meth:`_cancel_active`
        to close. Cleared again by ``_start_outgoing`` once the outcome is
        known.
        """
        self._active_conn = conn

    def _cancel_active(self) -> None:
        phase = self.transfer.phase
        if phase is Phase.INCOMING:
            self._resolve_accept(False, decline=True)
            return
        if phase is Phase.RINGING:
            self._cancel_requested = True
            self._fail_inplace("You cancelled the call")
            conn = self._active_conn
            if conn is not None:
                # Closing it ends the wait for their answer. Without this the
                # sender stayed parked in `dial` for the whole answer timeout
                # while a peer who *had* accepted streamed into a socket
                # nobody was reading — wedging them, not us.
                self.page.run_task(conn.close)
            return
        if phase is Phase.TRANSFERRING:
            self._cancel_requested = True
            self._fail_inplace("You stopped the transfer")
            conn = self._active_conn
            if conn is not None:
                self.page.run_task(conn.close)

    # ------------------------------------------------------------------
    # incoming transfer
    # ------------------------------------------------------------------

    def _validate_offer(self, header: dict) -> None:
        try:
            offer = protocol.parse_offer(header)
        except protocol.ProtocolError as ex:
            raise HandshakeError(str(ex)) from ex
        if not self.transfer.busy and self._pending_offer is None:
            self._pending_offer = offer

    def _on_ringing(self, header: dict) -> None:
        offer = self._pending_offer
        if offer is None:
            try:
                offer = protocol.parse_offer(header)
            except protocol.ProtocolError:
                return
            self._pending_offer = offer
        self._receiving_started = False
        # A cancel from an earlier outgoing transfer stays latched otherwise,
        # and would abort this one's very first chunk.
        self._cancel_requested = False
        try:
            self.transfer.incoming(
                IncomingOffer(
                    batch_id=offer.batch_id,
                    sender_id=offer.sender_id,
                    sender_name=offer.sender_name,
                    sender_host=offer.sender_ip or "unknown",
                    label=offer.label,
                    item_count=len(offer.items),
                    file_count=offer.file_count,
                    total=offer.total,
                )
            )
        except IllegalTransition:
            # A call raced our own outgoing one; lanlink already answered
            # busy for the peer, so there is nothing to show here.
            _log.info("dropped offer from %s: already %s", offer.sender_name, self.transfer.phase.value)
            self._pending_offer = None
            return
        incoming = self.transfer.state.incoming
        self._show_dialog(
            dialogs.incoming_dialog(
                incoming,  # type: ignore[arg-type]  (set by incoming() above)
                on_accept=lambda: self._resolve_accept(True),
                on_reject=lambda: self._resolve_accept(False, decline=True),
            )
        )
        self.page.run_task(self._watch_receipt_start)

    def _resolve_accept(self, accepted: bool, *, decline: bool = False) -> None:
        self._close_dialog()
        future = self._accept_future
        if future is not None and not future.done():
            future.set_result(accepted)
        offer = self._pending_offer
        self._pending_offer = None
        if accepted and offer is not None:
            self.transfer.begin_transfer(offer.total)
            self._paint_action()
        elif not accepted and decline:
            self._fail_inplace("You declined the transfer")

    async def _accept(self, header: dict) -> bool:
        future = asyncio.get_running_loop().create_future()
        self._accept_future = future
        try:
            return bool(await future)
        finally:
            self._accept_future = None

    async def _watch_receipt_start(self) -> None:
        """Consent was given but the sender never produced a payload.

        Without this, a caller that cancels between 'accept' and the first
        byte would leave the receiver staring at a 0% ring forever.
        """
        await asyncio.sleep(RECEIPT_START_TIMEOUT)
        if (
            self._receiving_started
            or self._cancel_requested
            or self.transfer.phase is not Phase.TRANSFERRING
            or self.transfer.state.direction is not Direction.IN
        ):
            return
        if self.transfer.state.progress and self.transfer.state.progress.done == 0:
            self._fail_inplace("The sender hung up before sending anything")

    async def _on_incoming(self, header: dict, conn) -> int:
        try:
            offer = protocol.parse_offer(header)
        except protocol.ProtocolError as ex:
            raise HandshakeError(str(ex)) from ex

        self._receiving_started = True
        # Same slot the sender uses, so "stop" can actually close the socket:
        # only one transfer is ever live (`is_busy` refuses an overlap).
        self._active_conn = conn
        started = time.time()
        dest = protocol.new_destination(
            self.dest_base,
            offer.sender_name,
            now=time.strftime("%Y%m%d-%H%M%S"),
        )
        writer = protocol.ManifestWriter(dest, offer.items)
        received = 0
        try:
            async for chunk in conn.iter_payload(offer.total):
                if self._cancel_requested:
                    raise _CancelledReceive()
                writer.write(chunk)
                received += len(chunk)
                self.transfer.advance(received, current_path=writer.current_path)
            writer.finish()
        except (_CancelledReceive, TransferAborted, OSError) as ex:
            writer.close()
            if isinstance(ex, _CancelledReceive) or self._cancel_requested:
                self._fail_inplace("You stopped the incoming transfer")
            else:
                _log.warning("receive aborted: %s", ex)
                self._fail_inplace("The sender stopped mid-transfer")
            raise TransferAborted(str(ex)) from ex
        except Exception:
            writer.close()
            raise
        finally:
            self._active_conn = None

        record = TransferRecord(
            direction=Direction.IN,
            peer_name=offer.sender_name,
            peer_host=offer.sender_ip or "",
            label=offer.label,
            item_count=offer.file_count,
            total_bytes=offer.total,
            started_at=started,
            finished_at=time.time(),
            ok=True,
        )
        self.records.insert(0, record)
        if self.transfer.phase is Phase.TRANSFERRING:
            self.transfer.finish(record)
            self._render_topbar()
            self._render_field()
            self._paint_action()
        _log.info("received %s (%d bytes) into %s", offer.label, received, dest)
        return writer.written

    def _on_session_error(self, exc: BaseException) -> None:
        """Called when a background call handler dies."""
        if self.transfer.phase in (Phase.TRANSFERRING, Phase.INCOMING, Phase.RINGING):
            self._fail_inplace(_friendly(exc))
