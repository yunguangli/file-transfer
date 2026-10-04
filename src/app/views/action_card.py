"""The action area: one card that re-dresses itself for every phase.

Compact layout puts this card under the radar; wide layout parks it in the
right-hand sidebar. Same builder either way — only the container changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import flet as ft

from .. import theme as T
from ..format import format_bytes, format_duration, format_percent, format_rate
from ..models import Direction, Phase
from ..transfer_vm import TransferState
from .common import centered, icon_badge, monogram, stat_pill


@dataclass(frozen=True)
class ActionHandlers:
    """Callbacks the action card can trigger (wired up by the controller)."""

    on_pick_files: Callable[[], None]
    on_clear_peer: Callable[[], None]
    on_transfer: Callable[[], None]
    on_change_selection: Callable[[], None]
    on_cancel: Callable[[], None]
    on_reset: Callable[[], None]


def build_action_card(
    state: TransferState,
    handlers: ActionHandlers,
    own: tuple[str, str] = ("me", "Me"),
) -> ft.Control:
    builders = {
        Phase.IDLE: _idle_card,
        Phase.SELECTED: _selected_card,
        Phase.PICKED: _picked_card,
        Phase.RINGING: _ringing_card,
        Phase.INCOMING: _incoming_card,
        Phase.TRANSFERRING: _transferring_card,
        Phase.DONE: _done_card,
        Phase.FAILED: _failed_card,
    }
    return builders[state.phase](state, handlers, own)


def _badge(icon, *, tint: str, bg: str) -> ft.Control:
    return icon_badge(icon, bg=bg, color=tint, size=44)


def _idle_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    return T.card(
        ft.Column(
            [
                ft.Row(
                    [
                        _badge(ft.Icons.RADAR, tint=T.PRIMARY, bg=T.alpha(T.PRIMARY, 0.14)),
                        ft.Column(
                            [
                                ft.Text(
                                    "No peer selected",
                                    size=15.5,
                                    weight=ft.FontWeight.W_700,
                                    color=T.INK,
                                ),
                                ft.Text(
                                    "Pick a nearby device to send files to.",
                                    size=12.5,
                                    color=T.MUTED,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                T.primary_button(
                    "Send files",
                    icon=ft.Icons.SEND,
                    disabled=True,
                ),
            ],
            spacing=14,
        )
    )


def _selected_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    peer = state.peer
    assert peer is not None
    return T.card(
        ft.Column(
            [
                ft.Row(
                    [
                        monogram(peer.id, peer.name, 46, ring=T.ACCENT),
                        ft.Column(
                            [
                                ft.Text(
                                    peer.name,
                                    size=15.5,
                                    weight=ft.FontWeight.W_700,
                                    color=T.INK,
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    peer.host,
                                    size=12.5,
                                    color=T.MUTED,
                                    font_family=T.FONT_MONO,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        stat_pill(
                            "Selected",
                            bg=T.alpha(T.PRIMARY, 0.12),
                            color=T.PRIMARY,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [
                        T.primary_button(
                            "Send files",
                            icon=ft.Icons.SEND,
                            on_click=lambda _e: h.on_pick_files(),
                        ),
                        T.ghost_button("Clear", on_click=lambda _e: h.on_clear_peer()),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=14,
        )
    )


def _picked_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    batch = state.batch
    assert batch is not None
    extras = []
    if batch.dir_count:
        extras.append(f"{batch.dir_count} folder{'s' if batch.dir_count != 1 else ''}")
    detail = " · ".join([f"{batch.file_count} file{'s' if batch.file_count != 1 else ''}"])
    if extras:
        detail = f"{detail} · {', '.join(extras)}"

    return T.card(
        ft.Column(
            [
                ft.Row(
                    [
                        ft.Column(
                            [
                                section_head("Ready to send"),
                                ft.Text(
                                    batch.label,
                                    size=16,
                                    weight=ft.FontWeight.W_700,
                                    color=T.INK,
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        stat_pill(
                            f"{format_bytes(batch.total_bytes)}",
                            icon=ft.Icons.DONUT_LARGE,
                            bg=T.alpha(T.ACCENT, 0.16),
                            color=T.INK_SOFT,
                        ),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Text(detail, size=12.5, color=T.MUTED),
                ft.Row(
                    [
                        T.primary_button(
                            "Transfer",
                            icon=ft.Icons.SEND,
                            on_click=lambda _e: h.on_transfer(),
                        ),
                        T.ghost_button(
                            "Change",
                            on_click=lambda _e: h.on_change_selection(),
                        ),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=12,
        )
    )


def _ringing_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    peer = state.peer
    name = peer.name if peer else "peer"
    return T.card(
        ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(
                            width=44,
                            height=44,
                            border_radius=ft.BorderRadius.all(22),
                            bgcolor=T.alpha(T.PRIMARY, 0.12),
                            alignment=ft.Alignment.CENTER,
                            content=ft.ProgressRing(
                                value=None,
                                width=22,
                                height=22,
                                stroke_width=2.5,
                                color=T.PRIMARY,
                            ),
                        ),
                        ft.Column(
                            [
                                ft.Text(
                                    f"Ringing {name}…",
                                    size=15.5,
                                    weight=ft.FontWeight.W_700,
                                    color=T.INK,
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    "Waiting for them to accept the transfer",
                                    size=12.5,
                                    color=T.MUTED,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                T.ghost_button(
                    "Cancel",
                    icon=ft.Icons.CLOSE,
                    color=T.DANGER,
                    bg=T.alpha(T.DANGER, 0.10),
                    expand=True,
                    on_click=lambda _e: h.on_cancel(),
                ),
            ],
            spacing=14,
        )
    )


def _incoming_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    offer = state.incoming
    if offer is None:
        return _idle_card(state, h, own)
    return T.card(
        ft.Row(
            [
                _badge(ft.Icons.FILE_DOWNLOAD, tint=T.ACCENT_DEEP, bg=T.alpha(T.ACCENT, 0.18)),
                ft.Column(
                    [
                        ft.Text(
                            "Incoming transfer",
                            size=15.5,
                            weight=ft.FontWeight.W_700,
                            color=T.INK,
                        ),
                        ft.Text(
                            f"{offer.label} from {offer.sender_name}",
                            size=12.5,
                            color=T.MUTED,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ],
                    spacing=2,
                    expand=True,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
    )


def _transferring_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    progress = state.progress
    peer = state.peer
    incoming = state.direction is Direction.IN
    if progress is None or peer is None:
        return _idle_card(state, h, own)
    if incoming and state.incoming is not None:
        file_count = state.incoming.file_count
    elif state.batch is not None:
        file_count = state.batch.file_count
    else:
        file_count = 0

    percent = progress.percent
    ring = ft.ProgressRing(
        value=percent,
        width=150,
        height=150,
        stroke_width=12,
        stroke_cap=ft.StrokeCap.ROUND,
        bgcolor=T.SURFACE_ALT,
        color=T.ACCENT,
    )
    ring_center = ft.Container(
        width=150,
        height=150,
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            [
                ft.Text(
                    format_percent(percent),
                    size=27,
                    weight=ft.FontWeight.W_800,
                    color=T.INK,
                ),
                ft.Text(
                    format_bytes(progress.done),
                    size=12,
                    color=T.MUTED,
                ),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )

    own_key, own_name = own
    avatar_pair = ft.Stack(
        [
            ft.Container(
                left=0,
                top=2,
                content=monogram(peer.id, peer.name, 42, ring=T.WHITE, ring_width=2),
            ),
            ft.Container(
                left=30,
                top=2,
                content=monogram(own_key, own_name, 42, ring=T.WHITE, ring_width=2),
            ),
        ],
        width=76,
        height=46,
    )

    footer = ft.Container(
        bgcolor=T.SURFACE,
        border_radius=ft.BorderRadius.all(20),
        padding=ft.Padding.symmetric(vertical=12, horizontal=14),
        content=ft.Row(
            [
                ft.Column(
                    [
                        ft.Text(
                            f"{'Receiving from' if incoming else 'Transferring to'} {peer.name}",
                            size=13,
                            weight=ft.FontWeight.W_600,
                            color=T.INK_SOFT,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Row(
                            [
                                stat_pill(
                                    format_duration(progress.elapsed),
                                    icon=ft.Icons.SCHEDULE,
                                    bg=T.WHITE,
                                    color=T.INK_SOFT,
                                ),
                                ft.Text(
                                    format_rate(progress.rate),
                                    size=14,
                                    weight=ft.FontWeight.W_700,
                                    color=T.INK,
                                ),
                            ],
                            spacing=8,
                            tight=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=6,
                    expand=True,
                ),
                avatar_pair,
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )

    return T.card(
        ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(
                            "File Transfer",
                            size=18,
                            weight=ft.FontWeight.W_700,
                            color=T.INK,
                        ),
                        ft.Container(expand=True),
                        T.round_icon_button(
                            ft.Icons.CLOSE,
                            on_click=lambda _e: h.on_cancel(),
                            tooltip="Cancel transfer",
                            bgcolor=T.SURFACE_ALT,
                            color=T.INK_SOFT,
                            size=36,
                            icon_size=17,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Column(
                    [
                        ft.Text(
                            state.label,
                            size=14,
                            weight=ft.FontWeight.W_600,
                            color=T.PRIMARY,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Text(
                            f"{format_bytes(progress.total)} · {file_count} file"
                            f"{'s' if file_count != 1 else ''}",
                            size=12.5,
                            color=T.MUTED,
                            no_wrap=True,
                        ),
                    ],
                    spacing=2,
                ),
                ft.Container(
                    content=ft.Stack(
                        [ring, ring_center],
                        alignment=ft.Alignment.CENTER,
                    ),
                    alignment=ft.Alignment.CENTER,
                    padding=ft.Padding.symmetric(vertical=6),
                ),
                footer,
                T.ghost_button(
                    "Cancel transfer",
                    icon=ft.Icons.CANCEL,
                    color=T.DANGER,
                    bg=T.alpha(T.DANGER, 0.10),
                    expand=True,
                    on_click=lambda _e: h.on_cancel(),
                ),
            ],
            spacing=12,
        )
    )


def _done_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    record = state.record
    if record is None:
        return _idle_card(state, h, own)
    incoming = record.direction is Direction.IN
    summary_bits = [
        f"{record.item_count} file{'s' if record.item_count != 1 else ''}",
        format_bytes(record.total_bytes),
        format_duration(record.duration),
    ]
    detail = (
        f"{'Received from' if incoming else 'Sent to'} {record.peer_name} · "
        + " · ".join(summary_bits)
    )
    primary_label = "Send more files" if not incoming else "Back to radar"

    return T.card(
        ft.Column(
            [
                centered(
                    icon_badge(
                        ft.Icons.CHECK_CIRCLE_ROUNDED,
                        bg=T.alpha(T.SUCCESS, 0.14),
                        color=T.SUCCESS,
                        size=56,
                    ),
                    ft.Text(
                        "Transfer complete",
                        size=18,
                        weight=ft.FontWeight.W_700,
                        color=T.INK,
                    ),
                    ft.Text(
                        detail,
                        size=13,
                        color=T.MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    spacing=8,
                ),
                T.primary_button(primary_label, on_click=lambda _e: h.on_reset()),
            ],
            spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
    )


def _failed_card(state: TransferState, h: ActionHandlers, own: tuple[str, str]) -> ft.Control:
    message = state.error or "Something went wrong."
    return T.card(
        ft.Column(
            [
                centered(
                    icon_badge(
                        ft.Icons.ERROR_ROUNDED,
                        bg=T.alpha(T.DANGER, 0.12),
                        color=T.DANGER,
                        size=56,
                    ),
                    ft.Text(
                        "Transfer ended",
                        size=18,
                        weight=ft.FontWeight.W_700,
                        color=T.INK,
                    ),
                    ft.Text(
                        message,
                        size=13,
                        color=T.MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    spacing=8,
                ),
                T.primary_button("Back", on_click=lambda _e: h.on_reset()),
            ],
            spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
    )


def section_head(value: str) -> ft.Text:
    return ft.Text(value, size=12.5, weight=ft.FontWeight.W_600, color=T.MUTED)
