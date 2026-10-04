"""Human-readable formatting for sizes, rates and time (UI copy only)."""

from __future__ import annotations

from typing import Optional

_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(count: float) -> str:
    """``1536`` -> ``1.5 KB``; matches the mock's compact style."""
    value = float(count)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} {_UNITS[-1]}"


def format_rate(bytes_per_second: Optional[float]) -> str:
    """``2.45e6`` -> ``2.4 MB/s``; ``None``/0 while the rate is unknown."""
    if not bytes_per_second:
        return "—"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: Optional[float]) -> str:
    """``14.2`` -> ``14s``; ``95`` -> ``1m 35s``."""
    if seconds is None:
        return "—"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "estimating…"
    return f"{format_duration(seconds)} left"


def format_percent(fraction: float) -> str:
    return f"{int(round(fraction * 100))}%"
