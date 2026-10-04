"""Radar screen: top bar, the scanning field, and the identity card."""

from __future__ import annotations

import flet as ft

from .. import theme as T
from ..radar_vm import RadarState, peer_slots
from .common import marker_height, monogram, peer_marker
from .radar_sweep import RadarSweep

try:  # optional: `uv add flet-spinkit` — the radar just skips the scan arc
    import flet_spinkit as spins
except ImportError:  # pragma: no cover - depends on the environment
    spins = None

PEER_AVATAR = 52.0
OWN_AVATAR = 62.0
RING_DIAMETERS = (0.94, 0.76, 0.58, 0.40)


def build_topbar(state: RadarState, *, on_back, on_bell) -> ft.Control:
    """Back (only when there is something to back out of) · title · history."""
    leading = (
        T.round_icon_button(
            ft.Icons.ARROW_BACK,
            on_click=on_back,
            tooltip="Back",
            bgcolor=T.WHITE,
        )
        if on_back
        else ft.Container(width=44, height=44)
    )
    trailing = T.round_icon_button(
        ft.Icons.HISTORY,
        on_click=on_bell,
        tooltip="Transfer history",
        bgcolor=T.WHITE,
    )
    title = ft.Column(
        [
            ft.Text(
                state.title,
                size=17,
                weight=ft.FontWeight.W_700,
                color=T.WHITE,
                text_align=ft.TextAlign.CENTER,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
            ft.Text(
                state.status_line,
                size=12,
                color=T.alpha(T.WHITE, 0.78),
                text_align=ft.TextAlign.CENTER,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        ],
        spacing=1,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )
    return ft.Row(
        [leading, ft.Container(content=title, expand=True), trailing],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=8,
    )


def _rings(size: float) -> list[ft.Control]:
    return [
        ft.Container(
            width=size * ratio,
            height=size * ratio,
            border_radius=ft.BorderRadius.all(size * ratio / 2),
            border=ft.Border.all(1.4, T.alpha(T.LINE, 0.9)),
            bgcolor=None,
        )
        for ratio in RING_DIAMETERS
    ]


def scan_arc(size: float) -> ft.Control | None:
    """Rotating arc, shown only while discovery has not found anyone yet.

    ``flet_spinkit`` is an optional dependency — without it the rings and the
    sweep beam carry the "scanning" read on their own.
    """
    if spins is None:
        return None
    return ft.Container(
        width=size,
        height=size,
        alignment=ft.Alignment.CENTER,
        content=spins.Ring(
            color=T.alpha(T.ACCENT_DEEP, 0.55),
            size=int(size * 0.64),
            line_width=4,
        ),
    )


def build_field(
    state: RadarState,
    *,
    size: float,
    sweep: RadarSweep,
    on_select,
) -> ft.Control:
    """The circular radar: rings, sweep beam, own avatar, peer markers."""
    slots = state_slots(state, size)
    markers: list[ft.Control] = []
    for peer in state.peers:
        x, y = slots.get(peer.id, (size / 2, size / 2))
        selected = peer.id == state.selected_id
        marker = peer_marker(
            peer.id,
            peer.name,
            size=PEER_AVATAR,
            selected=selected,
            on_click=lambda _e, pid=peer.id: on_select(pid),
            tooltip=f"{peer.name} — {peer.host}:{peer.port}",
        )
        markers.append(
            ft.Container(
                left=x - 116 / 2,
                top=y - marker_height(PEER_AVATAR) / 2 + 6,
                content=marker,
            )
        )

    own = ft.Container(
        left=size / 2 - (OWN_AVATAR + 8) / 2,
        top=size / 2 - (OWN_AVATAR + 8) / 2,
        content=monogram(
            state.identity.peer_id,
            state.identity.nickname or "me",
            OWN_AVATAR,
            ring=T.WHITE,
            ring_width=4,
        ),
    )

    layers: list[ft.Control] = list(_rings(size))
    if not state.peers:
        arc = scan_arc(size)
        if arc is not None:
            layers.append(arc)
    layers.extend([sweep, own, *markers])

    return ft.Container(
        width=size,
        height=size,
        border_radius=ft.BorderRadius.all(size / 2),
        bgcolor=T.SURFACE,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        shadow=[
            ft.BoxShadow(
                color=T.alpha("#1B2B4A", 0.22),
                blur_radius=46,
                spread_radius=4,
                offset=ft.Offset(0, 18),
            )
        ],
        content=ft.Stack(layers, alignment=ft.Alignment.CENTER),
    )


def state_slots(state: RadarState, size: float) -> dict[str, tuple[float, float]]:
    """Radar seats for the current peer set (thin seam so tests can assert
    layout math without building controls)."""
    return peer_slots([p.id for p in state.peers], size)


def build_identity_card(
    state: RadarState,
    *,
    name_field: ft.Control,
    on_copy_ip,
    on_settings,
    on_peer_list,
    copied: bool = False,
) -> ft.Control:
    """Bottom card of the mock: editable nickname, device line, IP pill."""
    identity = state.identity
    ip_pill = ft.Container(
        bgcolor=T.SURFACE_ALT,
        border_radius=ft.BorderRadius.all(T.RADIUS_CHIP),
        padding=ft.Padding.symmetric(vertical=6, horizontal=8),
        content=ft.Row(
            [
                ft.Container(
                    content=T.mono(
                        "Copied to clipboard" if copied else identity.ip,
                        size=15,
                        color=T.SUCCESS if copied else T.PRIMARY,
                    ),
                    padding=ft.Padding.only(left=10, right=4),
                    expand=True,
                ),
                T.round_icon_button(
                    ft.Icons.CONTENT_COPY,
                    on_click=on_copy_ip,
                    tooltip="Copy IP address",
                    bgcolor=T.WHITE,
                    color=T.PRIMARY,
                    size=34,
                    icon_size=16,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=4,
        ),
    )

    return T.card(
        ft.Column(
            [
                ft.Row(
                    [
                        T.round_icon_button(
                            ft.Icons.SETTINGS,
                            on_click=on_settings,
                            tooltip="Settings",
                            bgcolor=T.SURFACE_ALT,
                            color=T.INK_SOFT,
                            size=40,
                            icon_size=18,
                        ),
                        ft.Container(expand=True),
                        T.round_icon_button(
                            ft.Icons.GRID_VIEW,
                            on_click=on_peer_list,
                            tooltip="Peer list",
                            bgcolor=T.SURFACE_ALT,
                            color=T.INK_SOFT,
                            size=40,
                            icon_size=18,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(
                    content=name_field,
                    alignment=ft.Alignment.CENTER,
                    padding=ft.Padding.only(top=4, bottom=2),
                ),
                ft.Text(
                    f"This device · {identity.hostname}",
                    size=13,
                    color=T.MUTED,
                    text_align=ft.TextAlign.CENTER,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Container(
                    content=ip_pill,
                    alignment=ft.Alignment.CENTER,
                    padding=ft.Padding.only(top=10),
                ),
            ],
            spacing=4,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.Padding.all(18),
    )
