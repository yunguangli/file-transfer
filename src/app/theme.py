"""Design tokens for the myftp UI.

The palette was sampled from the original design mock, so the hex values
below are the source of truth — the mock itself is gone, don't go looking
for it. Keep this module free of logic beyond tiny derivations: views import
constants from here.
"""

from __future__ import annotations

import flet as ft

from .models import stable_hash

# --- palette ---------------------------------------------------------------

BG = "#647AB3"  # periwinkle app background
BG_DEEP = "#5C74B0"
BG_LIGHT = "#7793C3"
SURFACE = "#E8EEF6"  # light card / radar field
SURFACE_ALT = "#E7EDF5"  # chips, tracks, tiles
WHITE = "#FFFFFF"
PRIMARY = "#6888F8"  # file tiles, links, selection ring
PRIMARY_DEEP = "#4E66E0"
ACCENT = "#30E8F8"  # progress ring, live indicators
ACCENT_DEEP = "#0FBFD4"
INK = "#082840"  # headings / body
INK_SOFT = "#3E5468"
MUTED = "#8A97AD"
LINE = "#D4DDED"  # radar concentric circles
SUCCESS = "#2BB98A"
DANGER = "#EE5F6B"
AVATAR_FILLS = (
    "#6888F8",
    "#30E8F8",
    "#F06A8B",
    "#7C6CF0",
    "#2FB6C8",
    "#F5A05A",
    "#4E66E0",
    "#EF7FA8",
)

# --- geometry ------------------------------------------------------------

RADIUS_CARD = 28.0
RADIUS_SHEET = 26.0
RADIUS_TILE = 16.0
RADIUS_CHIP = 14.0

# Breakpoint under which the app switches to the compact (phone) layout.
COMPACT_BREAKPOINT = 720.0
# Max width of the app column on wide screens, so desktop looks composed
# instead of stretching a phone mock across 2560px.
APP_MAX_WIDTH = 620.0

FONT_MONO = "monospace"


def alpha(color: str, opacity: float) -> str:
    """Blend an opacity into a hex color as ``#aarrggbb`` (Flet's format).

    The leading ``#`` matters: without it the client cannot parse the string
    and silently falls back to the property's default (black).
    """
    opacity = 0.0 if opacity < 0 else 1.0 if opacity > 1 else opacity
    return f"#{int(round(opacity * 255)):02X}{color.lstrip('#').upper()}"


def avatar_fill(key: str) -> str:
    """Deterministic avatar color for a peer id/name (stable across runs)."""
    return AVATAR_FILLS[stable_hash(key) % len(AVATAR_FILLS)]


def initials(name: str) -> str:
    """1-2 letter monogram for a display name."""
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


# --- shared flet fragments -----------------------------------------------


def card_shadow(opacity: float = 0.16, blur: float = 30.0, y: float = 14.0):
    """Soft elevation shadow used by every raised card."""
    return [
        ft.BoxShadow(
            color=alpha("#1B2B4A", opacity),
            blur_radius=blur,
            spread_radius=-2,
            offset=ft.Offset(0, y),
        )
    ]


def chip_shadow(opacity: float = 0.12):
    return [
        ft.BoxShadow(
            color=alpha("#1B2B4A", opacity),
            blur_radius=12,
            spread_radius=0,
            offset=ft.Offset(0, 4),
        )
    ]


def card(
    content: ft.Control,
    *,
    bgcolor: str = WHITE,
    radius: float = RADIUS_CARD,
    padding: ft.PaddingValue = ft.Padding.all(20),
    shadow: bool = True,
    margin: ft.PaddingValue = None,
) -> ft.Container:
    """A raised rounded card — the app's universal surface."""
    return ft.Container(
        content=content,
        bgcolor=bgcolor,
        border_radius=ft.BorderRadius.all(radius),
        padding=padding,
        margin=margin,
        shadow=card_shadow() if shadow else None,
    )


def primary_button(
    label: str,
    *,
    on_click=None,
    icon=None,
    disabled: bool = False,
    expand: bool = True,
) -> ft.Control:
    btn = ft.Button(
        label,
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        style=ft.ButtonStyle(
            bgcolor=PRIMARY,
            color=WHITE,
            elevation=0,
            padding=ft.Padding.symmetric(vertical=14, horizontal=20),
            shape=ft.RoundedRectangleBorder(radius=18),
            text_style=ft.TextStyle(size=15, weight=ft.FontWeight.W_700),
        ),
    )
    return ft.Container(content=btn, expand=expand) if expand else btn


def ghost_button(
    label: str,
    *,
    on_click=None,
    icon=None,
    color: str = INK,
    bg: str = SURFACE_ALT,
    expand: bool = False,
) -> ft.Control:
    btn = ft.Button(
        label,
        icon=icon,
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor=bg,
            color=color,
            elevation=0,
            padding=ft.Padding.symmetric(vertical=14, horizontal=18),
            shape=ft.RoundedRectangleBorder(radius=18),
            text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
        ),
    )
    return ft.Container(content=btn, expand=expand) if expand else btn


def round_icon_button(
    icon,
    *,
    on_click=None,
    bgcolor: str = SURFACE_ALT,
    color: str = INK,
    size: float = 44.0,
    icon_size: float = 20.0,
    tooltip: str | None = None,
    shadow: bool = False,
) -> ft.Control:
    return ft.Container(
        width=size,
        height=size,
        border_radius=ft.BorderRadius.all(size / 2),
        bgcolor=bgcolor,
        alignment=ft.Alignment.CENTER,
        shadow=chip_shadow() if shadow else None,
        content=ft.Icon(icon, size=icon_size, color=color),
        tooltip=tooltip,
        on_click=on_click,
        ink=True,
    )


def section_title(value: str, *, size: float = 19.0) -> ft.Text:
    return ft.Text(
        value,
        size=size,
        weight=ft.FontWeight.W_700,
        color=INK,
    )


def muted(value: str, *, size: float = 13.0) -> ft.Text:
    return ft.Text(value, size=size, color=MUTED)


def mono(value: str, *, size: float = 15.0, color: str = PRIMARY, weight=None) -> ft.Text:
    return ft.Text(
        value,
        size=size,
        color=color,
        font_family=FONT_MONO,
        weight=weight or ft.FontWeight.W_600,
    )
