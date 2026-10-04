"""Shared visual atoms: avatars, name chips, radar markers, pills."""

from __future__ import annotations

import flet as ft

from .. import theme as T

CHIP_HEIGHT = 27.0
RING_WIDTH = 3.0
MARKER_WIDTH = 116.0


def monogram(
    key: str,
    name: str,
    size: float,
    *,
    ring: str | None = None,
    ring_width: float = RING_WIDTH,
) -> ft.Control:
    """Circular initial-avatar, optionally wrapped in a colored selection ring."""
    inner = ft.Container(
        width=size,
        height=size,
        border_radius=ft.BorderRadius.all(size / 2),
        bgcolor=T.avatar_fill(key),
        alignment=ft.Alignment.CENTER,
        content=ft.Text(
            T.initials(name),
            size=max(11.0, size * 0.34),
            weight=ft.FontWeight.W_700,
            color=T.WHITE,
        ),
    )
    if ring is None:
        return inner
    outer = size + ring_width * 2
    return ft.Container(
        width=outer,
        height=outer,
        border_radius=ft.BorderRadius.all(outer / 2),
        bgcolor=ring,
        padding=ft.Padding.all(ring_width),
        content=inner,
        shadow=T.chip_shadow(0.18),
    )


def name_chip(text: str, *, bg: str = T.WHITE, color: str = T.INK) -> ft.Control:
    return ft.Container(
        bgcolor=bg,
        border_radius=ft.BorderRadius.all(999),
        padding=ft.Padding.symmetric(vertical=5, horizontal=11),
        shadow=T.chip_shadow(0.10),
        content=ft.Text(
            text,
            size=12.5,
            weight=ft.FontWeight.W_600,
            color=color,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
            max_lines=1,
        ),
    )


def marker_height(size: float) -> float:
    """Total height of a peer marker: chip + gap + ringed avatar."""
    return CHIP_HEIGHT + 7 + size + RING_WIDTH * 2


def peer_marker(
    peer_id: str,
    name: str,
    *,
    size: float,
    selected: bool,
    on_click,
    tooltip: str | None = None,
) -> ft.Control:
    """Avatar + floating name label, as seen on the radar mock."""
    chip = name_chip(
        name,
        bg=T.PRIMARY if selected else T.WHITE,
        color=T.WHITE if selected else T.INK,
    )
    avatar = monogram(
        peer_id,
        name,
        size,
        ring=T.ACCENT if selected else T.WHITE,
    )
    return ft.Container(
        width=MARKER_WIDTH,
        alignment=ft.Alignment.CENTER,
        padding=ft.Padding.all(4),
        tooltip=tooltip,
        on_click=on_click,
        ink=True,
        content=ft.Column(
            [chip, avatar],
            spacing=7,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


def stat_pill(
    value: str,
    *,
    icon=None,
    bg: str = T.SURFACE_ALT,
    color: str = T.INK_SOFT,
    size: float = 12.5,
) -> ft.Control:
    children = [ft.Icon(icon, size=13, color=color)] if icon else []
    children.append(ft.Text(value, size=size, weight=ft.FontWeight.W_600, color=color))
    return ft.Container(
        bgcolor=bg,
        border_radius=ft.BorderRadius.all(T.RADIUS_CHIP),
        padding=ft.Padding.symmetric(vertical=5, horizontal=10),
        content=ft.Row(children, spacing=5, tight=True),
    )


def section_label(value: str) -> ft.Text:
    return ft.Text(value, size=13, weight=ft.FontWeight.W_600, color=T.MUTED)


def centered(*controls: ft.Control, spacing: float = 8) -> ft.Control:
    return ft.Column(
        list(controls),
        spacing=spacing,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        alignment=ft.MainAxisAlignment.CENTER,
    )


def icon_badge(icon, *, bg: str, color: str, size: float = 64) -> ft.Control:
    """Round tinted icon used by the empty/success/error cards."""
    return ft.Container(
        width=size,
        height=size,
        border_radius=ft.BorderRadius.all(size / 2),
        bgcolor=bg,
        alignment=ft.Alignment.CENTER,
        content=ft.Icon(icon, size=size * 0.5, color=color),
    )
