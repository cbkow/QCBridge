"""The replica's pixels without a screen capture (DESIGN-NOTES pixels).

Cycles: a Python RenderEngine that IS Cycles — a subclass of the bundled
`cycles.CyclesRender` under its own id — whose `view_draw` lets Cycles draw
and then reads the engine's own, scene-linear pixels back from the active
framebuffer (bit-identical to stock Cycles on Metal, noise-level on
Windows/GL; the 2026-10-26/27 campaign, private/research/overnight). The
scene's engine is mapped CYCLES → QCB_CYCLES on the replica only and mapped
back on stop, so a file never carries our id.

EEVEE / Workbench: those engines are built in and cannot be subclassed; a
POST_PIXEL draw handler renders the camera frame into a GPUOffScreen
(do_color_management=False: linear) at ≤15 Hz — a second, single-sample
render per call, within dE ~0.5 of the converged viewport.

Either way the frame goes to the shared-memory ring (frame_ring) the
encoder helper reads. Rules from the campaign: read the camera rectangle
only (shot mode), skip a draw whose centre hash is unchanged (70–90 % of
them), treat black-and-unchanged at session start as "not yet" (Cycles
kernels compiling), force preview_pixel_size 1 (Retina renders 2x2 blocks
otherwise), free every offscreen before quit.
"""
from __future__ import annotations

import hashlib
import os
import time

import bpy
import gpu
import numpy as np

from ..ring1 import ring_layout as rl
from . import frame_ring

_DEBUG = bool(os.environ.get("QCB_DEBUG"))

ENGINE_ID = "QCB_CYCLES"
_OFFSCREEN_MIN_INTERVAL = 1.0 / 15.0
_BACKOFF_AFTER = 8      # equal hashes in a row before readback thins out
_BACKOFF_EVERY = 4      # ... to one draw in this many

_st = {
    "ring": None,          # frame_ring.FrameRing
    "width": 2560,         # stream width cap (StreamParams.width)
    "shot_fn": None,       # () -> bool: shot mode on?
    "registered": False,
    "handler": None,
    "dirty": True,         # view_update since the last readback
    "unchanged_run": 0,
    "draw_i": 0,
    "last_hash": None,
    "seen_signal": False,  # a non-black frame has been read this session
    "probe": None,         # (t_host, seq)
    "frame_number": 0,
    "offscreen": None,
    "off_size": (0, 0),
    "off_last": 0.0,
    "retire_cb": None,     # (new_ring_name) -> None: the pixel path restarts the helper
    # stats for the pong / panel
    "published": 0, "skipped": 0, "read_ms": [], "errors": 0, "last_error": "",
    "engine": "", "size": (0, 0),
}


# ── engine mapping ─────────────────────────────────────────────────────────

def map_engine(value: str) -> str:
    return ENGINE_ID if (value == "CYCLES" and _st["registered"]) else value


def unmap_engine(value: str) -> str:
    return "CYCLES" if value == ENGINE_ID else value


def stream_width() -> int:
    return int(_st["width"])


def adopt_scene(scene) -> None:
    """Replica-only scene settings: our engine id in place of Cycles, GPU
    device, and 1:1 viewport pixels. A no-op until start() (the host and a
    screen-capture replica keep the file's settings)."""
    if _st["ring"] is None:
        return
    try:
        want = map_engine(scene.render.engine)
        if scene.render.engine != want:
            scene.render.engine = want
    except Exception as exc:
        _st["errors"] += 1
        _st["last_error"] = f"engine: {exc!r}"
    try:
        if scene.render.preview_pixel_size != "1":
            scene.render.preview_pixel_size = "1"
    except Exception:
        pass
    _st["seen_signal"] = False
    _st["last_hash"] = None
    _st["dirty"] = True


def _unadopt_all() -> None:
    for scene in bpy.data.scenes:
        try:
            if scene.render.engine == ENGINE_ID:
                scene.render.engine = "CYCLES"
        except Exception:
            pass


# ── the Cycles subclass ─────────────────────────────────────────────────────

def _make_engine_class():
    import cycles  # the bundled Cycles add-on (scripts/addons_core/cycles)

    class QCBCycles(cycles.CyclesRender):
        bl_idname = ENGINE_ID
        bl_label = "Cycles (QC Bridge)"

        def view_update(self, context, depsgraph):
            _st["dirty"] = True
            super().view_update(context, depsgraph)

        def view_draw(self, context, depsgraph):
            super().view_draw(context, depsgraph)
            try:
                _publish_from_framebuffer(context)
            except Exception as exc:  # never let the draw die
                _st["errors"] += 1
                _st["last_error"] = f"readback: {exc!r}"

    return QCBCycles


_engine_cls = None


# ── geometry ────────────────────────────────────────────────────────────────

def _camera_rect(region, rv3d):
    """The camera frame's rect in region pixels (x, y, w, h), even-sized,
    or None when the projection is stale or no camera."""
    from bpy_extras.view3d_utils import location_3d_to_region_2d

    scene = bpy.context.scene
    cam = scene.camera
    if cam is None or region is None or rv3d is None:
        return None
    mw = cam.matrix_world
    coords = [location_3d_to_region_2d(region, rv3d, mw @ p) for p in cam.data.view_frame(scene=scene)]
    if any(c is None for c in coords):
        return None
    xs = [c.x for c in coords]
    ys = [c.y for c in coords]
    x0, y0 = int(round(min(xs))), int(round(min(ys)))
    w, h = int(round(max(xs) - min(xs))), int(round(max(ys) - min(ys)))
    return x0, y0, w, h


def _clamp_rect(rect, vx, vy, vw, vh):
    x0, y0, w, h = rect
    rx0, ry0 = max(x0, 0), max(y0, 0)
    rw = min(w - (rx0 - x0), vw - rx0) & ~1
    rh = min(h - (ry0 - y0), vh - ry0) & ~1
    if rw < 16 or rh < 16:
        return None
    return vx + rx0, vy + ry0, rw, rh


def _in_shot_mode() -> bool:
    fn = _st["shot_fn"]
    return bool(fn()) if fn else False


# ── the readback (Cycles) ───────────────────────────────────────────────────

def _publish_from_framebuffer(context) -> None:
    ring = _st["ring"]
    if ring is None:
        return
    _st["engine"] = ENGINE_ID
    _st["draw_i"] += 1
    if (not _st["dirty"] and _st["unchanged_run"] >= _BACKOFF_AFTER
            and _st["draw_i"] % _BACKOFF_EVERY):
        return  # converged and nothing changed: thin the readbacks (Windows GL is sync)
    fb = gpu.state.active_framebuffer_get()
    vx, vy, vw, vh = fb.viewport_get()
    rect = None
    flags = 0
    if _in_shot_mode():
        cam_rect = _camera_rect(context.region, context.region_data)
        if cam_rect is not None:
            rect = _clamp_rect(cam_rect, vx, vy, vw, vh)
            flags |= rl.F_CAMERA_RECT
    if rect is None:
        rect = (vx, vy, vw & ~1, vh & ~1)
    x, y, w, h = rect
    if w < 16 or h < 16:
        return
    t0 = time.perf_counter()
    buf = fb.read_color(x, y, w, h, 4, 0, "FLOAT")
    t1 = time.perf_counter()
    arr = np.frombuffer(buf, dtype=np.float32).reshape(h, w, 4)
    _st["dirty"] = False
    _publish(arr, flags, t0, t1)


def _publish(arr: np.ndarray, flags: int, t0: float, t1: float) -> None:
    ring = _st["ring"]
    h, w = arr.shape[0], arr.shape[1]
    centre = arr[h // 4: 3 * h // 4: 16, w // 4: 3 * w // 4: 16]
    if not _st["seen_signal"]:
        if float(centre[..., :3].max()) <= 0.0:
            return  # startup black (kernels compiling): not a frame yet
        _st["seen_signal"] = True
        flags |= rl.F_FIRST_AFTER_LOAD
    digest = hashlib.blake2b(np.ascontiguousarray(centre).tobytes(), digest_size=8).digest()
    if digest == _st["last_hash"]:
        _st["unchanged_run"] += 1
        _st["skipped"] += 1
        return
    _st["last_hash"] = digest
    _st["unchanged_run"] = 0
    if not ring.fits(w, h):
        _regrow(w, h)
        ring = _st["ring"]
        if ring is None or not ring.fits(w, h):
            return
    _st["frame_number"] += 1
    ok = ring.publish(
        arr, flags=flags, frame_number=_st["frame_number"], t_read_start=t0, t_read_end=t1,
        probe_stamp=_st["probe"],
    )
    if ok:
        _st["published"] += 1
        _st["size"] = (w, h)
        ms = _st["read_ms"]
        ms.append((t1 - t0) * 1000.0)
        if len(ms) > 120:
            del ms[:-120]


def _regrow(w: int, h: int) -> None:
    """A frame taller than the ring: retire it and create a bigger one;
    the pixel path restarts the helper on the new name."""
    old = _st["ring"]
    width = max(_st["width"], w)
    try:
        new = frame_ring.FrameRing(width)
        if not new.fits(w, h):
            new.close()
            new = frame_ring.FrameRing(max(w, int(h * 4 / 3) + 2))
    except Exception as exc:
        _st["errors"] += 1
        _st["last_error"] = f"ring: {exc!r}"
        return
    _st["ring"] = new
    if old is not None:
        old.close()
    cb = _st["retire_cb"]
    if cb is not None:
        try:
            cb(new.name)
        except Exception as exc:
            _st["last_error"] = f"restart: {exc!r}"
    if _DEBUG:
        print(f"qcb ring regrown for {w}x{h}: {new.name}", flush=True)


# ── EEVEE / Workbench: offscreen ────────────────────────────────────────────

def _offscreen_draw() -> None:
    """POST_PIXEL handler on the replica's 3D view: for the built-in
    engines, render the camera frame (or the region) into an offscreen
    buffer, linear, at most 15 times a second, and publish it."""
    try:
        ring = _st["ring"]
        if ring is None:
            return
        scene = bpy.context.scene
        if scene.render.engine == ENGINE_ID:
            return  # Cycles publishes from view_draw
        now = time.monotonic()
        if now - _st["off_last"] < _OFFSCREEN_MIN_INTERVAL:
            return
        region = bpy.context.region
        area = bpy.context.area
        space = bpy.context.space_data
        if region is None or area is None or space is None or space.type != "VIEW_3D":
            return
        rv3d = space.region_3d
        if rv3d is None or region.width < 16 or region.height < 16:
            return
        _st["engine"] = scene.render.engine
        depsgraph = bpy.context.evaluated_depsgraph_get()
        cam = scene.camera
        flags = rl.F_OFFSCREEN_ENGINE
        if _in_shot_mode() and cam is not None:
            # The offscreen IS the camera frame: pick the size, no border.
            rx, ry = scene.render.resolution_x, scene.render.resolution_y
            aspect = (ry * scene.render.pixel_aspect_y) / max(1e-6, rx * scene.render.pixel_aspect_x)
            w = min(_st["width"], region.width) & ~1
            h = int(round(w * aspect)) & ~1
            if h > region.height:
                h = region.height & ~1
                w = int(round(h / max(1e-6, aspect))) & ~1
            view = cam.matrix_world.inverted()
            proj = cam.calc_matrix_camera(depsgraph, x=w, y=h, scale_x=1.0, scale_y=1.0)
            flags |= rl.F_CAMERA_RECT
        else:
            scale = min(1.0, _st["width"] / max(1, region.width))
            w = int(region.width * scale) & ~1
            h = int(region.height * scale) & ~1
            view = rv3d.view_matrix
            proj = rv3d.window_matrix
        if w < 16 or h < 16:
            return
        off = _st["offscreen"]
        if off is None or _st["off_size"] != (w, h):
            if off is not None:
                off.free()
            off = gpu.types.GPUOffScreen(w, h, format="RGBA16F")
            _st["offscreen"] = off
            _st["off_size"] = (w, h)
        t0 = time.perf_counter()
        off.draw_view3d(scene, bpy.context.view_layer, space, region, view, proj,
                        do_color_management=False, draw_background=True)
        buf = off.texture_color.read()
        t1 = time.perf_counter()
        arr = np.frombuffer(buf, dtype=np.float32).reshape(h, w, 4)
        _st["off_last"] = now
        _publish(arr, flags, t0, t1)
    except Exception as exc:
        _st["errors"] += 1
        _st["last_error"] = f"offscreen: {exc!r}"


def _free_offscreen() -> None:
    off = _st["offscreen"]
    _st["offscreen"] = None
    _st["off_size"] = (0, 0)
    if off is not None:
        try:
            off.free()
        except Exception:
            pass


# ── lifecycle ───────────────────────────────────────────────────────────────

def start(width: int, shot_fn, retire_cb=None) -> str | None:
    """Create the ring, register the engine, hook the offscreen handler.
    Returns the ring name for the helper, or None (no numpy/GPU)."""
    global _engine_cls
    _st["width"] = int(width)
    _st["shot_fn"] = shot_fn
    _st["retire_cb"] = retire_cb
    if _st["ring"] is None:
        try:
            _st["ring"] = frame_ring.FrameRing(_st["width"])
        except Exception as exc:
            _st["errors"] += 1
            _st["last_error"] = f"ring: {exc!r}"
            return None
    if not _st["registered"]:
        try:
            _engine_cls = _make_engine_class()
            bpy.utils.register_class(_engine_cls)
            _st["registered"] = True
        except Exception as exc:
            _st["errors"] += 1
            _st["last_error"] = f"engine register: {exc!r}"
            _engine_cls = None
    for scene in bpy.data.scenes:
        adopt_scene(scene)
    if _st["handler"] is None:
        _st["handler"] = bpy.types.SpaceView3D.draw_handler_add(_offscreen_draw, (), "WINDOW", "POST_PIXEL")
    return _st["ring"].name


def idle() -> None:
    """Host session over: release the GPU buffers, keep the ring and the
    engine (the next project re-adopts)."""
    _free_offscreen()
    _st["seen_signal"] = False
    _st["last_hash"] = None


def stop() -> None:
    _free_offscreen()
    if _st["handler"] is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_st["handler"], "WINDOW")
        except Exception:
            pass
        _st["handler"] = None
    _unadopt_all()
    if _st["registered"] and _engine_cls is not None:
        try:
            bpy.utils.unregister_class(_engine_cls)
        except Exception:
            pass
    _st["registered"] = False
    ring = _st["ring"]
    _st["ring"] = None
    if ring is not None:
        ring.close()
    _st["probe"] = None


def ring_name() -> str | None:
    ring = _st["ring"]
    return ring.name if ring is not None else None


def set_probe(t_host: float, seq: int) -> None:
    _st["probe"] = (t_host, seq)
    _st["dirty"] = True


def status() -> dict:
    ms = sorted(_st["read_ms"])
    return {
        "engine": _st["engine"], "size": list(_st["size"]),
        "published": _st["published"], "skipped": _st["skipped"],
        "read_ms": round(ms[len(ms) // 2], 2) if ms else None,
        "errors": _st["errors"], "last_error": _st["last_error"][:120],
        "ring": ring_name(),
    }
