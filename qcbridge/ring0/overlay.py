"""Burn-in status overlay — the honesty principle, in-band.

POST_PIXEL drawing survives show_overlays=False (verified 2026-07-24, the
magenta-canary run), so this travels inside the captured stream and is
visible in any receiver. The beauty window may be behind, never silently
wrong.
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
_probe_stamp: tuple[float, int] | None = None


def set_probe(t_host: float, seq: int) -> None:
    global _probe_stamp
    _probe_stamp = (t_host, seq)


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


def _draw_probe_strip() -> None:
    """Parity probe strip, bottom-left of the region just above the status
    text (the top is under the header when not in kiosk): opaque black
    backdrop one block wide on every side, white blocks for 1-bits."""
    region = bpy.context.region
    if _probe_stamp is None or region is None:
        return
    b = probe.BLOCK_PX
    bits = probe.encode_bits(*_probe_stamp)
    x = b  # backdrop starts at the region edge
    y0 = 48 + b  # clears the status text box (y 4..~36)
    y1 = y0 + b
    backdrop = [(0, y0 - b, x + len(bits) * b + b, y1 + b)]
    ones = [
        (x + i * b, y0, x + (i + 1) * b, y1) for i, bit in enumerate(bits) if bit
    ]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    shader.bind()
    gpu.state.blend_set("NONE")
    _draw_rects(shader, backdrop, (0.0, 0.0, 0.0, 1.0))
    _draw_rects(shader, ones, (1.0, 1.0, 1.0, 1.0))


def _draw() -> None:
    if _status_fn is None:
        return
    _draw_probe_strip()
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
    global _handle, _status_fn, _probe_stamp
    _probe_stamp = None
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    _status_fn = None
