"""Host hot-channel sampler: camera view + frame at 30 Hz.

Independent of the depsgraph path (docs/architecture.md §Host process model).
85 bytes per tick; conflate on the far side makes loss meaningless, so this
just samples and sends unconditionally.
"""

from __future__ import annotations

import bpy

from ..ring1 import protocol

_INTERVAL = 1.0 / 30.0

_transport = None


def _target_view():
    """The largest 3D viewport — the one the artist is actually working in."""
    best = None
    best_size = -1
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                size = area.width * area.height
                if size > best_size:
                    best, best_size = area, size
    return best


def _tick():
    if _transport is None:
        return None  # unregister
    area = _target_view()
    if area is not None:
        space = area.spaces.active
        rv3d = space.region_3d
        matrix = rv3d.view_matrix
        state = protocol.HotState(
            frame=bpy.context.scene.frame_current,
            view_matrix=tuple(v for row in matrix for v in row),
            lens=space.lens,
            clip_start=space.clip_start,
            clip_end=space.clip_end,
            is_persp=rv3d.is_perspective,
            camera=rv3d.view_perspective == "CAMERA",
            cam_zoom=rv3d.view_camera_zoom,
            cam_offset=tuple(rv3d.view_camera_offset),
        )
        _transport.send_hot(state.pack())
    return _INTERVAL


def start(transport) -> None:
    global _transport
    _transport = transport
    bpy.app.timers.register(_tick, first_interval=_INTERVAL, persistent=True)


def stop() -> None:
    global _transport
    _transport = None  # timer unregisters itself on next tick
