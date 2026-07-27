"""Kiosk mode — reduce the replica to pure viewport pixels (decision #13).

Port of the proven spike (spikes/stage0-capture-harness/win_kiosk.py),
restructured as a state machine serviced by the replica apply loop's tick —
deliberately NO timers of its own. Tier-3 bootstrap calls open_mainfile from
inside a timer callback; registering or owning separate timers around that
corrupted Blender's timer list (C crash in BLI_timer_execute, observed 5.2).
The apply tick survives file loads and is already an op-capable context.

Steps stay staggered (each op rebuilds screen state; the area is re-found
every time) and idempotent: window_fullscreen_toggle is a TOGGLE, guarded by
a module flag; area-maximize checks Screen.show_fullscreen.
"""

from __future__ import annotations

import os
import time

import bpy

_DEBUG = bool(os.environ.get("QCB_DEBUG"))

_want = False                # kiosk requested for this session
_os_fullscreen_done = False  # survives file loads AND session restarts:
                             # the OS window stays fullscreen either way
_steps: list[tuple[float, str]] = []  # (due monotonic time, step name)

_FIRST_DELAYS = (3.0, 4.5, 6.0, 8.5)     # window still settling after launch
_REASSERT_DELAYS = (0.5, 2.0, 2.7, 5.0)  # re-assert after a file load
_STEP_ORDER = ("os_fullscreen", "area_fullscreen", "strip_chrome", "verify")
_MAX_RETRIES = 3
_retries = 0


def _v3d():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        return window, area, region
    return None, None, None


def prepare_viewport() -> None:
    """Rendered shading, overlays off. The burn-in status overlay is our own
    POST_PIXEL drawing and survives show_overlays=False (verified 2026-07-24).
    Idempotent; safe with kiosk off (sync-only users still want rendered)."""
    _w, area, _r = _v3d()
    if not area:
        return
    space = area.spaces.active
    space.shading.type = "RENDERED"
    space.overlay.show_overlays = False


def enter(first: bool = True) -> None:
    """first=True uses the long launch-settle stagger; first=False (project
    load mid-session) re-engages quickly."""
    global _want, _retries
    _want = True
    _retries = 0
    prepare_viewport()
    _queue(_FIRST_DELAYS if first else _REASSERT_DELAYS)


def reassert() -> None:
    """Called after every bootstrap apply: open_mainfile reset the
    maximized-area/chrome state. Just re-queues steps — no timer traffic."""
    global _retries
    if _want:
        _retries = 0
        _queue(_REASSERT_DELAYS)


def cancel() -> None:
    """Session stop: pending steps become no-ops; the OS window is
    deliberately left as-is."""
    global _want
    _want = False
    _steps.clear()


def active() -> bool:
    return _want


def enter_now() -> None:
    """Manual toggle from a settled UI: run the steps back-to-back (the
    launch-time stagger existed for window settle, not for correctness)."""
    global _want
    _want = True
    _steps.clear()
    prepare_viewport()
    for name in _STEP_ORDER:
        _run_step(name)


def exit_now() -> None:
    """Restore a usable UI: un-maximize, bring back header/gizmos/overlays,
    leave OS fullscreen (toggled off too). Marks kiosk unwanted so bootstrap
    re-asserts don't fight the user's choice."""
    global _want, _os_fullscreen_done
    _want = False
    _steps.clear()
    window, area, region = _v3d()
    if area and window.screen.show_fullscreen:
        with bpy.context.temp_override(window=window, area=area, region=region):
            # back_to_previous, NOT screen_full_area: the toggle-out path
            # silently no-ops when invoked from a timer (probed on 5.2)
            bpy.ops.screen.back_to_previous()
    window, area, region = _v3d()  # re-find: screen swapped back
    if area:
        space = area.spaces.active
        space.show_region_header = True
        space.show_gizmo = True
        space.show_region_ui = True
        space.show_region_toolbar = True
        space.overlay.show_overlays = True
        area.tag_redraw()
    if _os_fullscreen_done and area:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.wm.window_fullscreen_toggle()
        _os_fullscreen_done = False


def service(now: float | None = None) -> None:
    """Run any due step. Called every replica apply tick (~15 ms)."""
    if not _want or not _steps:
        return
    if now is None:
        now = time.monotonic()
    due, name = _steps[0]
    if now < due:
        return
    _steps.pop(0)
    _run_step(name)


def _queue(delays: tuple[float, float, float]) -> None:
    now = time.monotonic()
    _steps.clear()
    _steps.extend((now + d, name) for d, name in zip(delays, _STEP_ORDER))


def _run_step(name: str) -> None:
    global _os_fullscreen_done
    window, area, region = _v3d()
    if _DEBUG:
        print(f"[{time.monotonic():.1f}] qcb kiosk step {name} (area={area is not None})", flush=True)
    if not area:
        return
    if name == "os_fullscreen":
        if not _os_fullscreen_done:
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.wm.window_fullscreen_toggle()
            _os_fullscreen_done = True
    elif name == "area_fullscreen":
        if not window.screen.show_fullscreen:
            with bpy.context.temp_override(window=window, area=area, region=region):
                # use_hide_panels ("true fullscreen") — owner's call
                # 2026-07-26, accepting a known Blender edge: drag-collapsing
                # a sidebar INSIDE this state can crash Blender's region code
                # (ACCESS_VIOLATION in ED_area_init via region_scale_modal,
                # seen on Windows). Kiosk screens aren't meant to be hand-
                # manipulated; use Ctrl+Alt+K to exit before poking the UI.
                bpy.ops.screen.screen_full_area(use_hide_panels=True)
    elif name == "strip_chrome":
        prepare_viewport()  # re-assert rendered/overlays-off post-load too
        _w, area, _r = _v3d()  # re-find: screen_full_area swapped the screen
        if area:
            space = area.spaces.active
            space.show_region_header = False
            space.show_gizmo = False
            space.show_region_ui = False       # N-panel
            space.show_region_toolbar = False  # T-panel
            area.tag_redraw()
    elif name == "verify":
        # The OS-fullscreen transition animates; a window re-layout at its
        # end can wipe an area-maximize applied mid-animation (measured on
        # macOS). Verify the end state and re-run the sequence until it
        # sticks — every step is idempotent, so retries are cheap.
        global _retries
        settled = (
            window.screen.show_fullscreen
            and not area.spaces.active.show_region_header
        )
        if not settled and _retries < _MAX_RETRIES:
            _retries += 1
            if _DEBUG:
                print(f"qcb kiosk verify FAILED — retry {_retries}", flush=True)
            _queue(_REASSERT_DELAYS)
    if _DEBUG:
        window, area, _r = _v3d()
        print(
            f"[{time.monotonic():.1f}] qcb kiosk after {name}: fullscreen={window.screen.show_fullscreen}"
            f" header={area.spaces.active.show_region_header}",
            flush=True,
        )
