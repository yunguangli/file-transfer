# AGENTS.md

Flet 1.0 desktop/web app (`myftp`) plus a stdlib-only P2P library in
`src/lanlink/`. Python >= 3.10, managed with `uv`. Not a git repo (yet).

It is a P2P file transfer app. It should be able to transfer any computer objjects to and from a peer.  
The entry point should be in ./src/main.py. Use modern architecture and Dataclass for easy maintainance and extensibility. 
use serena mcp for this dir
use flet mcp for syntex checking

## Commands

- `uv sync` — install deps first; no `.venv`/`uv.lock` exists yet.
- `uv run flet run` — desktop app with hot reload; `uv run flet run --web` for browser.
- `uv run pytest` — run all tests; `uv run pytest tests/test_main.py::test_increment` for one.
- `uv run flet build linux -v` — package (also: apk, ipa, macos, windows, web).

Always `uv run flet ...`, never bare `flet`: the `flet` on this machine's PATH
belongs to a different project's venv.

No linter, formatter, typechecker, or CI is configured — don't invent
lint/typecheck commands.

## Layout

- `[tool.flet.app] path = "src"` -> app entry is `src/main.py: main(page)`.
  Everything Flet loads lives under `src/` (assets: `src/assets/icon.png`).
- `src/lanlink/` — `discovery.py` (UDP multicast+broadcast beacons, port 47555),
  `session.py` (TCP framed handshake + paced payload, port 47554), `errors.py`.
  Public API is re-exported from `src/lanlink/__init__.py`; import from there.
- `tests/` — pytest config lives in `pyproject.toml`: `pythonpath = ["src"]`
  (tests import `lanlink.x` with no install step) and `asyncio_mode = "auto"`
  (plain `async def test_...`, no decorator).

## Boundaries

- `lanlink` must stay stdlib-only: never import Flet, `main`, or any app-layer
  module into it. Application-specific protocol semantics belong outside it.
  This rule is enforced by convention, documented in `src/lanlink/__init__.py`.
- `lanlink` is not wired into the app yet — `src/main.py` is still the stock
  Flet counter sample.

## Testing quirks

- Tests that use the `flet_app` fixture (Flet's auto-registered pytest plugin)
  run in device mode by default: they provision a Flutter test host via
  `flet-cli`, so the first run is slow (subsequent runs hit a cache). Requires
  the dev dependency group + Flutter toolchain. Tests that don't use the
  fixture are plain and fast.
- `tests/test_main.py::test_increment` drives the counter sample UI through
  `key="increment"`; it fails as soon as `src/main.py` is replaced. Update or
  remove it together with the UI change.
