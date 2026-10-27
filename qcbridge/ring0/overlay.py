"""Status overlay on the replica's own display — the honesty principle.

POST_PIXEL drawing survives show_overlays=False (verified 2026-07-24, the
magenta-canary run). Since 0.3 the stream is read back from the engine
BEFORE overlays draw, so this text is for whoever stands at the replica;
the viewer gets the same facts from the host panel, and the parity probe
strip is stamped into the published frame by ring0/frame_ring.
"""

from __future__ import annotations

from typing import Callable

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from ..ring1 import probe

_PAD = 8
_FONT_SIZE = 16

_handle = None
_status_fn: Callable[[], str] | None = None


def _quads(rects):
    tris = []
    for x0, y0, x1, y1 in rects:
        tris += [(x0, y0), (x1, y0), (x0, y1), (x1, y0), (x1, y1), (x0, y1)]
    return tris


def _draw_rects(shader, rects, color) -> None:
    if not rects:
        return
    batch = batch_for_shader(shader, "TRIS", {"pos": _quads(rects)})
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw() -> None:
    if _status_fn is None:
        return
    text = _status_fn()
    if not text:
        return
    font = 0
    blf.size(font, _FONT_SIZE)
    text_w, text_h = blf.dimensions(font, text)
    x, y = 12, 12
    quad = (
        (x - _PAD, y - _PAD),
        (x + text_w + _PAD, y - _PAD),
        (x - _PAD, y + text_h + _PAD),
        (x + text_w + _PAD, y + text_h + _PAD),
    )
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(
        shader, "TRIS",
        {"pos": (quad[0], quad[1], quad[2], quad[1], quad[3], quad[2])},
    )
    gpu.state.blend_set("ALPHA")
    shader.bind()
    shader.uniform_float("color", (0.0, 0.0, 0.0, 0.55))
    batch.draw(shader)
    gpu.state.blend_set("NONE")
    blf.position(font, x, y, 0)
    blf.color(font, 1.0, 1.0, 1.0, 1.0)
    blf.draw(font, text)


def enable(status_fn: Callable[[], str]) -> None:
    global _handle, _status_fn
    if _handle is not None:
        return
    _status_fn = status_fn
    _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")


def disable() -> None:
    global _handle, _status_fn
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    _status_fn = None
