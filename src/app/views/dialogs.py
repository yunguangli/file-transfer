"""Modal surfaces: consent prompt, file picker, peer list, settings, history.

Every builder here is a pure function of state + callbacks; the controller
owns the lifecycle (``page.show_dialog`` / ``page.pop_dialog``).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Sequence

import flet as ft

from .. import theme as T
from ..format import format_bytes, format_duration
from ..models import IncomingOffer, PeerView, TransferRecord
from .common import icon_badge, monogram, section_label, stat_pill

_SHEET_RADIUS = ft.RoundedRectangleBorder(
    radius=ft.BorderRadius.only(
        top_left=T.RADIUS_SHEET,
        top_right=T.RADIUS_SHEET,
        bottom_left=0,
        bottom_right=0,
    )
)
_DIALOG_RADIUS = ft.RoundedRectangleBorder(radius=24)

_EXT_ICONS = {
    ".pdf": "PICTURE_AS_PDF",
    ".png": "IMAGE",
    ".jpg": "IMAGE",
    ".jpeg": "IMAGE",
    ".gif": "IMAGE",
    ".webp": "IMAGE",
    ".svg": "IMAGE",
    ".heic": "IMAGE",
    ".mov": "MOVIE",
    ".mp4": "MOVIE",
    ".mkv": "MOVIE",
    ".mp3": "MUSIC_NOTE",
    ".wav": "MUSIC_NOTE",
    ".m4a": "MUSIC_NOTE",
    ".flac": "MUSIC_NOTE",
    ".zip": "FOLDER_ZIP",
    ".plist": "ARTICLE",
    ".txt": "ARTICLE",
    ".md": "ARTICLE",
    ".csv": "ARTICLE",
    ".json": "ARTICLE",
}


def _icon(name: str):
    return getattr(ft.Icons, name, None) or ft.Icons.DESCRIPTION


def file_icon(path: str):
    return _icon(_EXT_ICONS.get(Path(path).suffix.lower(), "DESCRIPTION"))


# --- incoming consent -----------------------------------------------------


def incoming_dialog(
    offer: IncomingOffer,
    *,
    on_accept: Callable[[], None],
    on_reject: Callable[[], None],
) -> ft.AlertDialog:
    """The accept/reject prompt a peer sees before any bytes move."""
    summary = ft.Row(
        [
            stat_pill(
                f"{offer.file_count} file{'s' if offer.file_count != 1 else ''}",
                icon=ft.Icons.DESCRIPTION,
                bg=T.alpha(T.PRIMARY, 0.12),
                color=T.PRIMARY,
            ),
            stat_pill(
                format_bytes(offer.total),
                icon=ft.Icons.DONUT_LARGE,
                bg=T.alpha(T.ACCENT, 0.18),
                color=T.INK_SOFT,
            ),
        ],
        spacing=8,
        tight=True,
    )
    body = ft.Column(
        [
            ft.Row(
                [
                    monogram(offer.sender_id, offer.sender_name, 48, ring=T.ACCENT),
                    ft.Column(
                        [
                            ft.Text(
                                offer.sender_name,
                                size=16,
                                weight=ft.FontWeight.W_700,
                                color=T.INK,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                offer.sender_host,
                                size=12.5,
                                color=T.MUTED,
                                font_family=T.FONT_MONO,
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
            ),
            ft.Text(
                offer.label,
                size=14,
                weight=ft.FontWeight.W_600,
                color=T.PRIMARY,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            summary,
            ft.Text(
                "Accept to receive these files on this device.",
                size=12.5,
                color=T.MUTED,
            ),
        ],
        spacing=12,
    )
    return ft.AlertDialog(
        modal=True,
        title=ft.Text("Incoming transfer", size=19, weight=ft.FontWeight.W_700, color=T.INK),
        content=body,
        bgcolor=T.WHITE,
        shape=_DIALOG_RADIUS,
        actions=[
            T.ghost_button(
                "Decline",
                icon=ft.Icons.CLOSE,
                color=T.DANGER,
                bg=T.alpha(T.DANGER, 0.10),
                expand=True,
                on_click=lambda _e: on_reject(),
            ),
            T.primary_button(
                "Accept",
                icon=ft.Icons.CHECK_ROUNDED,
                expand=True,
                on_click=lambda _e: on_accept(),
            ),
        ],
        actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        actions_padding=ft.Padding.only(top=18),
    )


# --- select file ----------------------------------------------------------


def recent_tile(path: str, *, on_click, exists: bool = True) -> ft.Control:
    name = Path(path).name
    return ft.Column(
        [
            ft.Container(
                width=74,
                height=74,
                border_radius=ft.BorderRadius.all(18),
                bgcolor=T.PRIMARY if exists else T.SURFACE_ALT,
                alignment=ft.Alignment.CENTER,
                opacity=1.0 if exists else 0.55,
                tooltip=path,
                on_click=lambda _e: on_click(path) if exists else None,
                ink=True,
                content=ft.Icon(
                    file_icon(path),
                    size=30,
                    color=T.WHITE if exists else T.MUTED,
                ),
            ),
            ft.Text(
                name,
                size=11,
                color=T.INK_SOFT,
                text_align=ft.TextAlign.CENTER,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
                max_lines=1,
                width=74,
            ),
        ],
        spacing=6,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )


def build_file_sheet(
    recent: Sequence[str],
    *,
    on_files: Callable[[], None],
    on_folder: Callable[[], None],
    on_recent: Callable[[str], None],
    on_close: Callable[[], None],
) -> ft.BottomSheet:
    """'Select File' sheet from the mock: recents row + browse row."""
    header = ft.Row(
        [
            ft.Text("Select File", size=20, weight=ft.FontWeight.W_700, color=T.INK),
            ft.Container(expand=True),
            T.round_icon_button(
                ft.Icons.CLOSE,
                on_click=lambda _e: on_close(),
                tooltip="Close",
                bgcolor=T.SURFACE_ALT,
                color=T.INK_SOFT,
                size=38,
                icon_size=18,
            ),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    if recent:
        tiles = ft.Row(
            [recent_tile(path, on_click=on_recent) for path in recent],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
    else:
        tiles = ft.Text(
            "Nothing yet — pick something to send.",
            size=12.5,
            color=T.MUTED,
        )

    recents = ft.Container(
        bgcolor=T.SURFACE,
        border_radius=ft.BorderRadius.all(22),
        padding=ft.Padding.all(16),
        content=ft.Column([section_label("Recently Accessed"), tiles], spacing=10),
    )

    browse = ft.Container(
        bgcolor=T.SURFACE,
        border_radius=ft.BorderRadius.all(18),
        padding=ft.Padding.symmetric(vertical=8, horizontal=14),
        content=ft.Row(
            [
                ft.Text("Browse", size=15, weight=ft.FontWeight.W_700, color=T.INK),
                ft.Container(expand=True),
                T.round_icon_button(
                    ft.Icons.DESCRIPTION,
                    on_click=lambda _e: on_files(),
                    tooltip="Pick files",
                    bgcolor=T.WHITE,
                    color=T.PRIMARY,
                    size=44,
                ),
                T.round_icon_button(
                    ft.Icons.FOLDER_OPEN,
                    on_click=lambda _e: on_folder(),
                    tooltip="Pick a folder",
                    bgcolor=T.WHITE,
                    color=T.PRIMARY,
                    size=44,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )

    content = ft.Container(
        width=560,
        bgcolor=T.WHITE,
        border_radius=ft.BorderRadius.only(
            top_left=T.RADIUS_SHEET, top_right=T.RADIUS_SHEET
        ),
        padding=ft.Padding.all(20),
        content=ft.Column([header, recents, browse], spacing=16),
    )
    return ft.BottomSheet(
        content=content,
        shape=_SHEET_RADIUS,
        show_drag_handle=True,
        dismissible=True,
        scrollable=True,
        bgcolor=T.WHITE,
        barrier_color=T.alpha("#0B1B33", 0.35),
    )


# --- peer list ------------------------------------------------------------


def build_peer_list_sheet(
    peers: Sequence[PeerView],
    selected_id: str | None,
    *,
    on_select: Callable[[str], None],
    on_close: Callable[[], None],
) -> ft.BottomSheet:
    """Grid button on the identity card: pick a peer from a list instead."""
    header = ft.Row(
        [
            ft.Text("Nearby peers", size=20, weight=ft.FontWeight.W_700, color=T.INK),
            ft.Container(expand=True),
            T.round_icon_button(
                ft.Icons.CLOSE,
                on_click=lambda _e: on_close(),
                tooltip="Close",
                bgcolor=T.SURFACE_ALT,
                color=T.INK_SOFT,
                size=38,
                icon_size=18,
            ),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    if not peers:
        body: ft.Control = ft.Text(
            "No peers found yet. Keep the app open on the other device.",
            size=13,
            color=T.MUTED,
        )
    else:
        rows: list[ft.Control] = []
        for peer in peers:
            selected = peer.id == selected_id
            rows.append(
                ft.Container(
                    bgcolor=T.alpha(T.PRIMARY, 0.10) if selected else T.SURFACE,
                    border_radius=ft.BorderRadius.all(18),
                    padding=ft.Padding.symmetric(vertical=10, horizontal=12),
                    on_click=lambda _e, pid=peer.id: on_select(pid),
                    ink=True,
                    content=ft.Row(
                        [
                            monogram(
                                peer.id,
                                peer.name,
                                40,
                                ring=T.ACCENT if selected else None,
                            ),
                            ft.Column(
                                [
                                    ft.Text(
                                        peer.name,
                                        size=14.5,
                                        weight=ft.FontWeight.W_700,
                                        color=T.INK,
                                        no_wrap=True,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                    ),
                                    ft.Text(
                                        peer.host,
                                        size=12,
                                        color=T.MUTED,
                                        font_family=T.FONT_MONO,
                                    ),
                                ],
                                spacing=1,
                                expand=True,
                            ),
                            stat_pill(
                                "Selected" if selected else "Select",
                                bg=T.WHITE if selected else T.SURFACE_ALT,
                                color=T.PRIMARY if selected else T.INK_SOFT,
                            ),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        body = ft.Column(rows, spacing=10)

    content = ft.Container(
        width=560,
        bgcolor=T.WHITE,
        border_radius=ft.BorderRadius.only(
            top_left=T.RADIUS_SHEET, top_right=T.RADIUS_SHEET
        ),
        padding=ft.Padding.all(20),
        content=ft.Column(
            [header, body],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
        ),
    )
    return ft.BottomSheet(
        content=content,
        shape=_SHEET_RADIUS,
        show_drag_handle=True,
        dismissible=True,
        scrollable=True,
        bgcolor=T.WHITE,
        barrier_color=T.alpha("#0B1B33", 0.35),
    )


# --- settings / history ---------------------------------------------------


def settings_dialog(
    *,
    peer_id: str,
    nickname: str,
    destination: str,
    recent_count: int,
    on_clear_recents: Callable[[], None],
    on_close: Callable[[], None],
) -> ft.AlertDialog:
    def _row(label: str, value: str, *, mono: bool = False) -> ft.Control:
        return ft.Row(
            [
                ft.Text(label, size=13, color=T.MUTED, width=120),
                ft.Text(
                    value,
                    size=13,
                    color=T.INK,
                    weight=ft.FontWeight.W_600,
                    font_family=T.FONT_MONO if mono else None,
                    expand=True,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        )

    body = ft.Column(
        [
            _row("Nickname", nickname),
            _row("Peer id", peer_id[:12] + "…", mono=True),
            _row("Receive to", destination, mono=True),
            _row("Recent files", str(recent_count)),
            ft.Text(
                "Change your nickname any time on the main screen — peers see it "
                "as soon as you type.",
                size=12.5,
                color=T.MUTED,
            ),
        ],
        spacing=12,
    )
    return ft.AlertDialog(
        modal=True,
        title=ft.Text("Settings", size=19, weight=ft.FontWeight.W_700, color=T.INK),
        content=body,
        bgcolor=T.WHITE,
        shape=_DIALOG_RADIUS,
        actions=[
            T.ghost_button(
                "Clear recents",
                color=T.DANGER,
                bg=T.alpha(T.DANGER, 0.10),
                expand=True,
                on_click=lambda _e: on_clear_recents(),
            ),
            T.primary_button("Done", expand=True, on_click=lambda _e: on_close()),
        ],
        actions_alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        actions_padding=ft.Padding.only(top=18),
    )


def history_dialog(
    records: Sequence[TransferRecord],
    *,
    on_close: Callable[[], None],
) -> ft.AlertDialog:
    """Bell icon: what happened this session (destination is in Settings)."""
    if not records:
        body: ft.Control = ft.Column(
            [
                icon_badge(
                    ft.Icons.HISTORY,
                    bg=T.SURFACE,
                    color=T.MUTED,
                    size=54,
                ),
                ft.Text(
                    "No transfers yet this session.",
                    size=13,
                    color=T.MUTED,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
    else:
        rows: list[ft.Control] = []
        for rec in list(records)[:8]:
            ok = rec.ok
            rows.append(
                ft.Container(
                    bgcolor=T.SURFACE,
                    border_radius=ft.BorderRadius.all(16),
                    padding=ft.Padding.symmetric(vertical=10, horizontal=12),
                    content=ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.CHECK_CIRCLE_ROUNDED
                                if ok
                                else ft.Icons.ERROR_ROUNDED,
                                size=20,
                                color=T.SUCCESS if ok else T.DANGER,
                            ),
                            ft.Column(
                                [
                                    ft.Text(
                                        f"{rec.verb} {rec.label}",
                                        size=13.5,
                                        weight=ft.FontWeight.W_700,
                                        color=T.INK,
                                        no_wrap=True,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                    ),
                                    ft.Text(
                                        f"{rec.peer_name} · "
                                        f"{format_bytes(rec.total_bytes)} · "
                                        f"{format_duration(rec.duration)}",
                                        size=11.5,
                                        color=T.MUTED,
                                        no_wrap=True,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                    ),
                                ],
                                spacing=1,
                                expand=True,
                            ),
                            ft.Text(
                                time.strftime(
                                    "%H:%M", time.localtime(rec.finished_at or rec.started_at)
                                ),
                                size=11.5,
                                color=T.MUTED,
                                font_family=T.FONT_MONO,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        body = ft.Column(rows, spacing=8, scroll=ft.ScrollMode.AUTO)

    return ft.AlertDialog(
        modal=True,
        title=ft.Text(
            "Transfer history", size=19, weight=ft.FontWeight.W_700, color=T.INK
        ),
        content=body,
        bgcolor=T.WHITE,
        shape=_DIALOG_RADIUS,
        actions=[T.primary_button("Close", expand=True, on_click=lambda _e: on_close())],
        actions_padding=ft.Padding.only(top=18),
    )
