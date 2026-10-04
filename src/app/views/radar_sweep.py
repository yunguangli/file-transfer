"""A radar sweep you can drop into any Flet app: a rotating, translucent
beam drawn on a canvas, animated by the control itself.


Usage
-----

The whole integration is one line — it starts when it is mounted and stops
when it is taken off the page::

    import flet as ft
    from app.views.radar_sweep import RadarSweep

    def main(page: ft.Page) -> None:
        page.add(RadarSweep(size=320))

    ft.run(main)

Every knob is optional and Python-side::

    RadarSweep(
        size=320,           # side of the square the beam is drawn in
        color="#30E8F8",    # accent; defaults to ``theme.ACCENT``
        tail=math.pi / 2,   # trail length in radians (default: quarter turn)
        speed=SWEEP_STEP,   # radians per frame; negative sweeps backwards
        tick=0.1,           # seconds per frame (0.1 -> 10 fps)
        angle=0.0,          # start angle: radians, clockwise-positive
    )

Layered the way a radar wants it — background, beam, then whatever has to
sit on top of the sweep (the beam never covers them)::

    ft.Stack(
        [
            *rings,           # plain Containers with a border
            RadarSweep(size=side),
            own_avatar,
            *peer_markers,
        ],
        width=side,
        height=side,
    )

Driving it by hand when you need to::

    sweep.advance()               # one frame of geometry, no client round trip
    sweep.resize(480)             # re-fit; keeps angle and the running task
    sweep.stop(), sweep.start()   # pause / resume (start() is idempotent)

You never schedule the animation yourself: Flet calls ``did_mount()`` when
the control enters the page tree (starts the task) and ``will_unmount()``
when it leaves (cancels it). A control that stays in the tree across an
update gets neither callback, so repainting the tree around it — a new peer,
a new nickname, a window resize — neither restarts nor duplicates the
animation. ``tests/test_radar_sweep.py`` pins exactly those patch deltas.


How it is made
--------------

* **The beam is geometry, not a rotated widget.** A gradient ``Container``
  behind a ``Rotate`` never showed up: ``Container.gradient`` defaults to
  ``BlendMode.MODULATE`` and the rotation happens on the client. Solid
  ``Paint`` on a ``flet.canvas`` is the compositing path that renders.
* **The tail is one path.** ``sweep_shapes(size, angle)`` walks from the
  centre out to the leading edge and back around a quarter turn in
  ``SWEEP_SLICES * ARC_STEPS`` interpolated steps, then closes. Drawn as
  separate bands, each band is anti-aliased on its own and the shared edges
  show up as hairline seams. The fill is a linear gradient anchored on the
  leading edge pointing tailward, so the bright end always rides the edge
  and the wedge dies out behind the sweep; a separate stroked path draws the
  leading edge itself.
* **The animation is one coroutine per control.** ``_loop()`` sleeps
  ``tick``, calls ``_tick()`` — advance the angle, rebuild the shapes, push
  ``self.update()`` — and returns for good the moment that update raises,
  which is exactly what an unmounted control does (``.page`` raises instead
  of returning ``None``).
* **The knobs never reach the client.** ``size``/``color``/``tail``/
  ``speed``/``tick``/``angle`` are dataclass fields carrying
  ``metadata={"skip": True}``, which keeps them out of the control's sparse
  prop set: the only thing serialized is ``content``, the canvas. The client
  knows this control as a ``Container`` and has never heard of a "speed".
* **Isolated on purpose.** ``@ft.control(isolated=True)`` excludes the
  control from parent-driven update traversal, so a repaint of the tree
  around it cannot race with — or swallow — a beam frame; every frame is
  pushed by the control itself (Flet's own rule: a control that calls
  ``self.update()`` in its methods should be isolated).

The only non-standard line is the ``..theme`` import, which supplies the
default accent — pass ``color=`` and you can delete it, leaving a file that
depends on nothing but Flet.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import field
from typing import Optional

import flet as ft
import flet.canvas as cv

from .. import theme as T

TAU = 2 * math.pi
SWEEP_TAIL = math.pi / 2  # how far the trail reaches behind the leading edge
SWEEP_SLICES = 8  # slivers used to fake the fade (alpha, not a gradient)
ARC_STEPS = 6  # line segments per slice arc
SWEEP_TICK = 0.1  # seconds between frames (10 fps)
SWEEP_STEP = TAU / 40  # radians per frame -> one full turn every 4s


def sweep_shapes(
    size: float,
    angle: float,
    *,
    color: str = T.ACCENT,
    tail: float = SWEEP_TAIL,
) -> list:
    """The beam for one frame: a translucent tail wedge plus its leading edge.

    ``angle`` is the leading edge in radians, clockwise-positive; the tail
    trails behind it and fades out backwards.
    """
    c = size / 2.0
    r = size / 2.0 - 1.0

    steps = SWEEP_SLICES * ARC_STEPS
    points: list = [cv.Path.MoveTo(c, c)]
    for j in range(steps + 1):
        a = angle - tail * j / steps
        points.append(cv.Path.LineTo(c + r * math.cos(a), c + r * math.sin(a)))
    points.append(cv.Path.Close())

    # Gradient axis: a line through the leading edge, running tailward — so
    # the whole leading edge sits on stop 0 (bright) and the wedge dies out
    # behind the sweep. A linear gradient cannot be exactly radial about the
    # centre, but the part it gets wrong is hidden behind the avatar anyway.
    bx, by = math.cos(angle), math.sin(angle)
    tx, ty = math.sin(angle), -math.cos(angle)  # tailward: angle decreases
    a0 = 0.65 * r  # where on the leading edge the axis is anchored
    axis = 1.25 * r
    begin = (c + a0 * bx, c + a0 * by)
    end = (begin[0] + axis * tx, begin[1] + axis * ty)

    wedge = cv.Path(
        points,
        paint=ft.Paint(
            color=T.alpha(color, 0.45),
            gradient=ft.PaintLinearGradient(
                begin=ft.Offset(*begin),
                end=ft.Offset(*end),
                colors=[T.alpha(color, 0.55), T.alpha(color, 0.0)],
                color_stops=[0.0, 1.0],
            ),
        ),
    )
    # the bright radial line the sweep reads from
    edge = cv.Path(
        [
            cv.Path.MoveTo(c, c),
            cv.Path.LineTo(c + r * bx, c + r * by),
        ],
        paint=ft.Paint(
            color=T.alpha(color, 0.9),
            style=ft.PaintingStyle.STROKE,
            stroke_width=2.5,
            stroke_cap=ft.StrokeCap.ROUND,
        ),
    )
    return [wedge, edge]


# A Python-only field is one with `skip` metadata: it is a normal constructor
# argument, but it never enters `_values`, so it is neither patched to the
# client nor diffed against it. `flet.controls.base_control.skip_field()` is
# the same field with a `None` default; these keep real defaults instead.
def _local(default):
    return field(default=default, metadata={"skip": True})


@ft.control(isolated=True)
class RadarSweep(ft.Container):
    """A square, self-animating radar beam.

    Args:
        size: side of the square the beam is drawn in.
        color: accent; defaults to the app theme.
        tail: trail length in radians (default a quarter turn).
        speed: radians per frame — negative sweeps the other way.
        tick: seconds per frame.
        angle: starting angle in radians.

    ``isolated=True`` because this control pushes its own updates every
    frame: it is excluded from parent-driven update traversal, so a radar
    repaint around it cannot race with (or swallow) a tick.
    """

    size: float = _local(400.0)
    color: Optional[str] = _local(None)
    tail: float = _local(SWEEP_TAIL)
    speed: float = _local(SWEEP_STEP)
    tick: float = _local(SWEEP_TICK)
    angle: float = _local(0.0)

    def init(self) -> None:
        # The client only ever sees this canvas; the knobs above stay here.
        self._canvas = cv.Canvas(
            width=self.size,
            height=self.size,
            shapes=sweep_shapes(
                self.size, self.angle, color=self.color or T.ACCENT, tail=self.tail
            ),
        )
        self.content = self._canvas
        self._task = None

    # --- geometry ------------------------------------------------------

    def _shapes(self) -> list:
        return sweep_shapes(
            self.size,
            self.angle,
            color=self.color or T.ACCENT,
            tail=self.tail or SWEEP_TAIL,
        )

    def advance(self) -> None:
        """Move the beam one frame forward and rebuild its geometry.

        Pure Python — no client round trip — so it is safe to call from a
        test, or to drive from somewhere else if you want manual control.
        """
        step = self.speed if self.speed else SWEEP_STEP
        self.angle = (self.angle + step) % TAU
        self._canvas.shapes = self._shapes()

    def resize(self, size: float) -> bool:
        """Re-fit the beam to a new square, keeping its angle and its task.

        Returns False when the size is already close enough (half a pixel),
        which is the common case — the caller repaints on every state change.
        """
        if size is None or abs(size - self.size) <= 0.5:
            return False
        self.size = size
        self._canvas.width = size
        self._canvas.height = size
        self._canvas.shapes = self._shapes()
        try:
            self.update()
        except RuntimeError:
            pass  # not on the page yet — the first mount sends the canvas
        return True

    # --- animation -----------------------------------------------------

    def _tick(self) -> bool:
        """One frame. False means "off the page", which ends the loop."""
        self.advance()
        try:
            self.update()
        except RuntimeError:
            # `.page` raises instead of returning None while unmounted, and
            # `update()` refuses frozen controls: either way this control can
            # no longer paint itself. `did_mount()` restarts it on return.
            return False
        return True

    async def _loop(self) -> None:
        while True:
            delay = self.tick if self.tick and self.tick > 0 else SWEEP_TICK
            await asyncio.sleep(delay)
            if not self._tick():
                return

    def start(self) -> None:
        """Begin animating; a no-op while a task is already running."""
        if self._task is not None and not self._task.done():
            return
        self._task = self.page.run_task(self._loop)

    def stop(self) -> None:
        """Stop animating; a safe no-op if it never started."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()

    def did_mount(self) -> None:
        self.start()

    def will_unmount(self) -> None:
        self.stop()
