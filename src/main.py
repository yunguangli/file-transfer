"""myftp — a LAN radar for peer-to-peer transfers.

Entry point per :tool.flet.app: ``flet run`` loads ``src/main.py`` and calls
``main(page)``. All real work lives in :mod:`app.controller`; this module only
hands the page over to it.

``App.start`` is a coroutine, so it is scheduled with ``page.run_task`` rather
than awaited here — ``main`` itself must stay synchronous.


Radar sweep
-----------

The beam is the reusable custom control ``app.views.radar_sweep.RadarSweep``
— one file, documented top to bottom (usage, knobs, layering, internals).

**How this app uses it** — the controller plants a single instance in the
field and re-fits it when the radar changes size; nothing else knows about
the animation::

    from .views.radar_sweep import RadarSweep

    if self._sweep is None:
        self._sweep = RadarSweep(size=side)   # once, before page.add()
    else:
        self._sweep.resize(side)              # same instance: angle + task live on

It is stacked inside the field (``radar_view.build_field``) as: rings →
optional ``flet_spinkit`` scan arc (only while no peers are known, and only
if that optional dependency is installed) → ``RadarSweep`` → own avatar →
peer markers, so the beam always renders under the avatars and markers.

**How it is made**:

* ``@ft.control(isolated=True)`` + a canvas — ``sweep_shapes(size, angle)``
  hand-builds one frame: a quarter-turn tail wedge (one path, so its bands
  cannot show hairline seams) filled with a linear gradient that runs
  tailward from the leading edge, plus a stroked leading edge at ``angle``
  (radians, clockwise-positive). A rotated gradient ``Container`` was tried
  first and never showed up: ``Container.gradient`` defaults to
  ``BlendMode.MODULATE`` and the rotation is applied client-side, so solid
  ``Paint`` on a canvas is the compositing path that verifiably renders.
  ``theme.alpha`` must emit ``#aarrggbb`` — without the leading ``#`` Flet
  falls back to black.
* It starts its own task in ``did_mount()`` and cancels it in
  ``will_unmount()``; the loop sleeps ``tick`` (0.1 s → 10 fps), advances
  ``angle`` by ``speed`` (``SWEEP_STEP = 2π/40`` — 9°/frame, one turn every
  4 s) and calls its own ``update()``. Flet fires neither hook for a control
  that stays put across an update, so repainting the radar neither restarts
  nor duplicates the loop, and the loop ends for good the moment an update
  raises — which is what an unmounted control does (``.page`` raises rather
  than returning ``None``).
* ``size``/``color``/``tail``/``speed``/``tick``/``angle`` are fields with
  ``skip`` metadata: they stay out of the control's sparse prop set, so the
  only thing sent to the Dart side is ``content`` — the canvas. The client
  knows this control as a ``Container`` and has never heard of a "speed".
"""

from __future__ import annotations

import flet as ft

from app.controller import App


def main(page: ft.Page) -> None:
    page.run_task(App(page).start)


if __name__ == "__main__":
    ft.run(main)
