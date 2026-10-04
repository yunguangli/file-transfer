"""The radar sweep as a standalone control: geometry, lifecycle, wire size.

``RadarSweep`` replaced a controller-owned canvas plus a 10 fps task in
``App``. These tests pin what is easy to regress and invisible until the app
is actually run: the shape of the beam, that the animation knobs stay off
the wire, and that mount/unmount really start and stop the task.
"""

import asyncio
import math
from dataclasses import fields

import flet as ft
import pytest
from flet.controls.base_control import BaseControl
from flet.controls.object_patch import ObjectPatch

from app import theme as T
from app.models import Identity
from app.radar_vm import RadarState
from app.views import radar_sweep as S, radar_view
from app.views.radar_sweep import RadarSweep, sweep_shapes

KNOBS = {"size", "color", "tail", "speed", "tick", "angle"}

FIELD_STATE = RadarState(
    identity=Identity(
        peer_id="me", nickname="panger", hostname="studio", ip="10.0.0.1"
    )
)


# --- geometry -------------------------------------------------------------


def test_the_tail_is_a_single_path_with_a_bright_leading_edge():
    wedge, edge = sweep_shapes(200, 0.0)

    # MoveTo(centre) + (slices * steps + 1) LineTo + Close — one path, so
    # there are no anti-aliased hairline seams between bands.
    assert len(wedge.elements) == 1 + (S.SWEEP_SLICES * S.ARC_STEPS + 1) + 1
    assert wedge.elements[0]._type == "MoveTo"
    assert (wedge.elements[0].x, wedge.elements[0].y) == (100.0, 100.0)
    assert wedge.elements[-1]._type == "Close"
    # tail ends a quarter turn behind the leading edge (angle 0 -> up)
    assert (wedge.elements[-2].x, wedge.elements[-2].y) == pytest.approx((100.0, 1.0))

    assert len(edge.elements) == 2  # centre -> leading edge
    assert (edge.elements[1].x, edge.elements[1].y) == pytest.approx((199.0, 100.0))
    assert edge.paint.style is ft.PaintingStyle.STROKE
    assert edge.paint.stroke_width == 2.5


def test_the_fade_runs_tailward_from_the_leading_edge():
    wedge, _ = sweep_shapes(200, 0.0)
    gradient = wedge.paint.gradient

    # anchored 65% out along the leading edge, extending tailward
    assert (gradient.begin.x, gradient.begin.y) == pytest.approx((164.35, 100.0))
    assert gradient.end.y < gradient.begin.y  # opposite the sweep at angle 0
    assert gradient.colors == [T.alpha(T.ACCENT, 0.55), T.alpha(T.ACCENT, 0.0)]
    assert gradient.color_stops == [0.0, 1.0]


def test_tail_length_and_colour_are_knobs():
    wedge, edge = sweep_shapes(200, 0.0, color="#123456", tail=math.pi)

    # a half-turn tail reaches all the way around to the opposite side
    assert (wedge.elements[-2].x, wedge.elements[-2].y) == pytest.approx((1.0, 100.0))
    assert edge.paint.color == T.alpha("#123456", 0.9)
    assert wedge.paint.gradient.colors[0] == T.alpha("#123456", 0.55)


# --- wire size ------------------------------------------------------------


def test_the_animation_knobs_never_reach_the_client():
    sweep = RadarSweep(
        size=320, color="#ff0000", tail=0.5, speed=0.4, tick=0.02, angle=1.25
    )

    # The client knows this control only as a Container: the canvas is the
    # single prop that gets serialized, everything else is Python-side.
    assert set(sweep._values) == {"content"}
    assert sweep._values["content"] is sweep._canvas
    assert sweep._c == "Container"
    assert sweep.is_isolated()

    knobs = {f.name for f in fields(sweep) if f.name in KNOBS}
    assert knobs == KNOBS
    assert all(f.metadata.get("skip") for f in fields(sweep) if f.name in KNOBS)


# --- animation ------------------------------------------------------------


def test_advance_moves_the_beam_and_rebuilds_the_geometry():
    sweep = RadarSweep(size=200, angle=0.0, speed=S.SWEEP_STEP)
    before = sweep._canvas.shapes

    sweep.advance()

    assert sweep.angle == pytest.approx(S.SWEEP_STEP)
    assert sweep._canvas.shapes is not before
    tip = sweep._canvas.shapes[1].elements[1]  # leading edge endpoint
    assert (tip.x, tip.y) == pytest.approx(
        (100 + 99 * math.cos(S.SWEEP_STEP), 100 + 99 * math.sin(S.SWEEP_STEP))
    )


def test_advance_wraps_at_a_full_turn():
    sweep = RadarSweep(size=200, angle=S.TAU - 0.01, speed=0.02)
    sweep.advance()
    assert sweep.angle == pytest.approx(0.01)
    assert 0.0 <= sweep.angle < S.TAU


def test_a_frame_off_the_page_ends_the_loop():
    """`.page` raises while unmounted — `_tick()` must report, not blow up."""
    sweep = RadarSweep(size=100)
    assert sweep._tick() is False


def test_resize_refits_in_place_and_keeps_the_angle():
    sweep = RadarSweep(size=200, angle=1.25)

    assert sweep.resize(200.3) is False  # half a pixel of drift: no work
    assert sweep.resize(320) is True
    assert (sweep.size, sweep._canvas.width, sweep._canvas.height) == (320, 320, 320)
    assert sweep.angle == 1.25  # resizing never rewinds the beam
    assert sweep.resize(320) is False


def test_stop_is_safe_before_anything_started():
    sweep = RadarSweep(size=100)
    sweep.stop()
    assert sweep._task is None


# --- lifecycle ------------------------------------------------------------


class FakeFuture:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        return True

    def done(self):
        return False


class FakePage:
    """Enough of a Page for `start()`: run_task, and nothing else."""

    def __init__(self):
        self.tasks = []

    def run_task(self, coro_fn):
        coro = coro_fn()
        coro.close()  # never awaited — the lifecycle is what we are testing
        task = FakeFuture()
        self.tasks.append(task)
        return task


def test_the_mount_hooks_own_the_animation_task(monkeypatch):
    sweep = RadarSweep(size=100)
    page = FakePage()
    monkeypatch.setattr(RadarSweep, "page", property(lambda self: page))

    sweep.did_mount()
    assert len(page.tasks) == 1
    assert sweep._task is page.tasks[0]

    sweep.did_mount()  # a second mount must not stack a second task
    assert len(page.tasks) == 1

    task = sweep._task
    sweep.will_unmount()
    assert task.cancelled
    assert sweep._task is None

    sweep.did_mount()  # back on the page -> a fresh task
    assert len(page.tasks) == 2


async def test_the_loop_runs_frames_until_the_control_leaves_the_page():
    sweep = RadarSweep(size=100, tick=0.001)
    frames = 0

    def frame():  # stand-in for `_tick`
        nonlocal frames
        frames += 1
        return frames < 3

    sweep._tick = frame
    await asyncio.wait_for(sweep._loop(), timeout=5.0)
    assert frames == 3


# --- what the session will do with it -------------------------------------
#
# Flet's session runs `will_unmount()` on every control the patch removed and
# `did_mount()` on every control it added, skipping any control that shows up
# on both sides (session.py). Those two hooks are what start and stop our
# task, so the patch deltas are the contract this control depends on —
# pinned here rather than left to a screenshot.


def _lifecycle_callbacks(old, new, *, frozen: bool = False):
    """(will_unmount, did_mount) lists, exactly as the session derives them."""
    _, added, removed = ObjectPatch.from_diff(
        old, new, control_cls=BaseControl, frozen=frozen
    )
    added_ids = {c._i for c in added}
    removed_ids = {c._i for c in removed}
    return (
        [c for c in removed if c._i not in added_ids],
        [c for c in added if c._i not in removed_ids],
    )


def _field(sweep, state, size=300.0):
    return radar_view.build_field(
        state, size=size, sweep=sweep, on_select=lambda _pid: None
    )


def test_entering_the_field_starts_the_sweep():
    """First mount: the sweep is an addition, so did_mount() runs."""
    sweep = RadarSweep(size=300)
    unmount, mount = _lifecycle_callbacks(None, _field(sweep, FIELD_STATE))
    assert sweep not in unmount
    assert sweep in mount


def test_repainting_the_field_never_stops_the_sweep():
    """`build_field` makes fresh containers every time; the beam must not care.

    A `will_unmount()` here would cancel the task on every peer refresh.
    """
    sweep = RadarSweep(size=300)
    first = _field(sweep, FIELD_STATE)
    for next_field in (_field(sweep, FIELD_STATE), _field(sweep, FIELD_STATE)):
        for frozen in (False, True):
            unmount, _ = _lifecycle_callbacks(first, next_field, frozen=frozen)
            assert sweep not in unmount, f"frozen={frozen}"
        first = next_field


def test_a_replaced_sweep_is_stopped_and_a_new_one_started():
    old, new = RadarSweep(size=300), RadarSweep(size=300)
    unmount, mount = _lifecycle_callbacks(_field(old, FIELD_STATE), _field(new, FIELD_STATE))
    assert old in unmount  # -> will_unmount() -> task.cancel()
    assert new in mount  # -> did_mount() -> a fresh task
