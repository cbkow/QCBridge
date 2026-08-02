"""Replica apply loop (sync-protocol.md §Replica apply loop).

bpy.app.timers tick ~15 ms, budget ~10 ms: drain the hot slot first, then pop
cold items until the budget is spent. frame_set is NOT a cheap write —
latest-wins absorbs scrub floods and a rate limit keeps scrubbing from
starving tier-1. All failures are counted and surfaced in the burn-in
overlay — the replica may be behind, never silently wrong.
"""

from __future__ import annotations

import collections
import json
import time

import bpy
from mathutils import Matrix

from ..ring1 import protocol
from . import bootstrap, kiosk, pixel_path, tier2_io
from .identity import UUID_PROP

_TICK = 0.015
_BUDGET_S = 0.010
_FRAME_MIN_INTERVAL = 0.1

_SCANNED_COLLECTIONS = (
    "objects", "lights", "cameras", "materials", "worlds", "scenes", "meshes",
    "curves", "node_groups", "images", "collections", "actions", "shape_keys",
    "lattices", "armatures",
)

_transport = None
_last_hot: bytes | None = None
_last_frame_apply = 0.0
_last_redraw_poke = 0.0

# Surfaced by the overlay (module-level: the overlay callback reads these).
stats = {
    "seq": 0,
    "gaps": 0,
    "applied_t1": 0,
    "apply_errors": 0,
    "unknown_uuid": 0,
    "applied_t2": 0,
    "bootstraps": 0,
    "unmapped_paths": 0,
    "host_ended": False,
    "applying": "",  # datablock name during an indivisible apply
    "last_error": "",
}

_seq_tracker = protocol.SeqTracker()
_uuid_map: dict[str, bpy.types.ID] = {}
_reassembler = protocol.Reassembler()
_inbox: collections.deque = collections.deque()  # polled-but-unprocessed msgs
_blob_by_uuid: dict[str, str] = {}   # uuid → latest in-flight blob id
_pending_t2: tuple[dict, bytes] | None = None  # applied on the NEXT tick so
                                               # the "⟳ applying" label gets a redraw first
_mappings: list = []
_project_dir_local = ""  # mapped-local project dir from the last bootstrap —
                         # tier-2 arrivals resolve their relative paths against it
_host_goodbye = False    # set from the IO thread; handled on the next tick
_new_session = False     # set from the IO thread on a fresh handshake
_zoom_offset = 0.0       # remote nudge, applied ON TOP of the synced camera
                         # zoom (written from the IO thread — a float store
                         # is atomic; the apply tick just reads it)

# Shot mode (decision #12, built 2026-07-26): replica locks to the camera
# frame, fitted to the canvas, passepartout opaque — and ignores host
# navigation. The "crop" is optical: camera view + passepartout 1.0 + a
# computed fill zoom keeps ddagrab fully zero-copy. Frame scrubbing still
# follows; the zoom nudge stacks on top of the fitted zoom.
_shot = {
    "on": False,
    "fit_pending": False,
    "measure_at": 0.0,   # measurement needs a redraw after entering camera view
    "fit_zoom": None,
    "saved_pp": None,    # (show_passepartout, alpha) to restore on exit
    "restore": False,
}


def nudge_zoom(delta: float, reset: bool = False) -> None:
    """Called from the transport IO thread (control channel) — no bpy."""
    global _zoom_offset, _last_hot
    _zoom_offset = 0.0 if reset else _zoom_offset + delta
    _last_hot = None  # bust the hot dedup: a static view means identical
                      # packets, and the new offset must still re-apply


def set_shot_mode(on: bool) -> None:
    """Called from the transport IO thread — flags only, no bpy."""
    global _last_hot
    if _shot["on"] == on:
        return
    _shot["on"] = on
    if on:
        _shot["fit_pending"] = True
        _shot["fit_zoom"] = None
    else:
        _shot["restore"] = True
    _last_hot = None


def _zoom_to_fac(zoom: float) -> float:
    return ((2 ** 0.5 + zoom / 50.0) ** 2) / 4.0


def _fac_to_zoom(fac: float) -> float:
    return ((4.0 * fac) ** 0.5 - 2 ** 0.5) * 50.0


def _camera_border(area, rv3d):
    """The camera frame's rect in region pixels (via frustum projection —
    the stage-0 spike's pixel-exact method), or None while the perspective
    matrix is stale (needs a redraw after a view change)."""
    from bpy_extras.view3d_utils import location_3d_to_region_2d

    scene = bpy.context.scene
    cam = scene.camera
    if cam is None:
        return None
    region = next(r for r in area.regions if r.type == "WINDOW")
    mw = cam.matrix_world
    points = [mw @ p for p in cam.data.view_frame(scene=scene)]
    coords = [location_3d_to_region_2d(region, rv3d, p) for p in points]
    if any(c is None for c in coords):
        return None
    xs = [c.x for c in coords]
    ys = [c.y for c in coords]
    return (max(xs) - min(xs), max(ys) - min(ys), region.width, region.height)


def _service_shot(area) -> None:
    space = area.spaces.active
    rv3d = space.region_3d
    now = time.monotonic()

    if _shot["restore"]:
        _shot["restore"] = False
        cam = bpy.context.scene.camera
        if cam is not None and _shot["saved_pp"] is not None:
            cam.data.show_passepartout, cam.data.passepartout_alpha = _shot["saved_pp"]
        _shot["saved_pp"] = None
        _shot["fit_zoom"] = None
        return

    if not _shot["on"]:
        return
    if rv3d.view_perspective != "CAMERA":
        rv3d.view_perspective = "CAMERA"
        _shot["measure_at"] = now + 0.3  # let a redraw land first
        area.tag_redraw()
        return
    cam = bpy.context.scene.camera
    if cam is not None:
        if _shot["saved_pp"] is None:
            _shot["saved_pp"] = (
                cam.data.show_passepartout, cam.data.passepartout_alpha
            )
        if not cam.data.show_passepartout or cam.data.passepartout_alpha < 1.0:
            cam.data.show_passepartout = True
            cam.data.passepartout_alpha = 1.0
    if _shot["fit_pending"] and now >= _shot["measure_at"]:
        border = _camera_border(area, rv3d)
        if border is not None and border[0] > 1 and border[1] > 1:
            bw, bh, rw, rh = border
            scale = min(rw / bw, rh / bh)
            fitted = _fac_to_zoom(_zoom_to_fac(rv3d.view_camera_zoom) * scale)
            _shot["fit_zoom"] = max(-30.0, min(600.0, fitted))
            _shot["fit_pending"] = False
        else:
            area.tag_redraw()  # matrix still stale; try next tick
    if _shot["fit_zoom"] is not None:
        target = max(-30.0, min(600.0, _shot["fit_zoom"] + _zoom_offset))
        if abs(rv3d.view_camera_zoom - target) > 1e-4:
            rv3d.view_camera_zoom = target
            area.tag_redraw()
        if tuple(rv3d.view_camera_offset) != (0.0, 0.0):
            rv3d.view_camera_offset[0] = 0.0
            rv3d.view_camera_offset[1] = 0.0


def _rebuild_uuid_map() -> None:
    _uuid_map.clear()
    for coll_name in _SCANNED_COLLECTIONS:
        for db in getattr(bpy.data, coll_name):
            stamped = db.get(UUID_PROP)
            if stamped:
                _uuid_map[stamped] = db


def _resolve(uuid: str):
    db = _uuid_map.get(uuid)
    if db is not None:
        try:
            db.name  # touch: a removed datablock raises
            return db
        except ReferenceError:
            pass
    _rebuild_uuid_map()
    return _uuid_map.get(uuid)


def _layer_collection_for(coll):
    def walk(lc):
        if lc.collection == coll:
            return lc
        for child in lc.children:
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(bpy.context.view_layer.layer_collection)


def _write_rna(db, path: str, value) -> None:
    # "@" paths: view-layer state applied by setter, not RNA (shadow.py)
    if path == "@hide":  # the object eye toggle
        db.hide_set(bool(value))
        return
    if path == "@scene_camera":
        db.camera = bpy.data.objects.get(value) if value else None
        return
    if path in ("@lc_exclude", "@lc_hide"):
        lc = _layer_collection_for(db)
        if lc is None:
            raise ValueError(f"{db.name!r} not in the active view layer")
        if path == "@lc_exclude":
            lc.exclude = bool(value)
        else:
            lc.hide_viewport = bool(value)
        return
    if path.startswith("["):
        # Custom property — the host json.dumps'd the name into the path,
        # so dots/quotes in prop names round-trip exactly. Checked before
        # the dotted-path split below for that same reason.
        db[json.loads(path[1:-1])] = value
        return
    if "." in path:
        parent_path, attr = path.rsplit(".", 1)
        parent = db.path_resolve(parent_path)
    else:
        parent, attr = db, path
    current = getattr(parent, attr)
    if isinstance(value, list) and not isinstance(current, (list, str)):
        try:
            setattr(parent, attr, value)
            return
        except (TypeError, ValueError):
            current[:] = value  # bpy_prop_array slice assign
            return
    setattr(parent, attr, value)


def _apply_t1(header: dict, payload: bytes) -> None:
    db = _resolve(header["uuid"])
    if db is None:
        stats["unknown_uuid"] += 1
        return
    changes = json.loads(payload.decode("utf-8"))
    for path, value in changes:
        try:
            _write_rna(db, path, value)
            stats["applied_t1"] += 1
        except Exception as exc:
            stats["apply_errors"] += 1
            stats["last_error"] = f"{header['uuid']}.{path}: {exc!r}"


def _apply_tombstone(header: dict) -> None:
    global _last_hot
    db = _resolve(header["uuid"])
    if db is None:
        return
    try:
        bpy.data.batch_remove((db,))
    except Exception:
        stats["apply_errors"] += 1
    _uuid_map.pop(header["uuid"], None)
    _last_hot = None  # same camera-view risk as a t2 apply (see above)


def _apply_pending_t2() -> None:
    global _pending_t2, _project_dir_local, _last_hot
    header, blob = _pending_t2
    _pending_t2 = None
    blob_tag = header["blob"]["id"].replace(".", "-")
    try:
        if header.get("kind") == "boot":
            errors, unmapped, _project_dir_local = bootstrap.apply_mainfile(
                blob, blob_tag, header.get("project_dir", ""), _mappings
            )
            stats["bootstraps"] += 1
            stats["apply_errors"] += errors
            stats["unmapped_paths"] = unmapped
            stats["host_ended"] = False
            from . import session  # deferred: session imports this module
            session.on_project_loaded()
        else:
            _name, errors = tier2_io.apply_blob(
                blob, blob_tag, _mappings, _project_dir_local
            )
            stats["applied_t2"] += 1
            stats["apply_errors"] += errors
        _rebuild_uuid_map()
    except Exception as exc:
        stats["apply_errors"] += 1
        stats["last_error"] = f"{header.get('kind')} {header.get('name')}: {exc!r}"
    finally:
        stats["applying"] = ""
        # A t2/boot apply can replace the very camera the viewport is
        # looking through — batch_remove knocks the view out of CAMERA
        # perspective. A static host view means every hot packet is
        # identical, so without busting the dedup the view would stay
        # broken until someone presses Num0 on the replica (field report
        # 2026-08-02 — the documented _last_hot gotcha). Shot mode re-fits
        # for the same reason: its zoom was measured against the old
        # camera datablock.
        _last_hot = None
        if _shot["on"]:
            _shot["fit_pending"] = True
            _shot["measure_at"] = time.monotonic() + 0.3


def _process_cold(deadline: float) -> None:
    # One message at a time through _inbox so a mid-batch stop (a completed
    # tier-2 blob deferring its apply) never drops already-polled messages.
    global _pending_t2
    while time.monotonic() < deadline and _pending_t2 is None:
        if not _inbox:
            transport = _transport
            if transport is None:
                return
            items = transport.poll_cold(8)
            if not items:
                return
            _inbox.extend(items)
        header, payload = _inbox.popleft()
        seq = header.get("seq")
        if seq is not None:
            _seq_tracker.observe(seq)
            stats["seq"] = _seq_tracker.last_seen or 0
            stats["gaps"] = _seq_tracker.gaps
        kind = header.get("kind")
        if kind == "t1":
            _apply_t1(header, payload)
        elif kind == "tomb":
            _apply_tombstone(header)
        elif kind in ("t2", "boot"):
            uuid = header.get("uuid", "")
            blob_id = header["blob"]["id"]
            stale = _blob_by_uuid.get(uuid)
            if stale and stale != blob_id:
                _reassembler.drop(stale)  # superseded mid-flight
            _blob_by_uuid[uuid] = blob_id
            done = _reassembler.feed(header, payload)
            if done is not None:
                _blob_by_uuid.pop(uuid, None)
                # Defer the indivisible apply one tick: label first.
                stats["applying"] = done[0].get("name", "?")
                _pending_t2 = done
                area = _target_view()
                if area:
                    area.tag_redraw()


def _target_view():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                return area
    return None


def _apply_hot(packed: bytes) -> None:
    global _last_frame_apply
    state = protocol.unpack_hot(packed)
    if state is None:
        return
    area = _target_view()
    if area is None:
        return
    space = area.spaces.active
    rv3d = space.region_3d
    if _shot["on"]:
        # Shot mode: the replica holds the fitted camera frame — host
        # navigation is deliberately ignored; only time still follows.
        scene = bpy.context.scene
        now = time.monotonic()
        global _last_frame_apply
        if (
            state.frame != scene.frame_current
            and now - _last_frame_apply >= _FRAME_MIN_INTERVAL
        ):
            scene.frame_set(state.frame)
            _last_frame_apply = now
        return
    if state.camera:
        # Camera view: the camera object defines the view — setting the
        # matrix would fight it. Zoom = host's zoom + the remote nudge
        # offset, so the replica can be punched in/out independently while
        # still following. Passepartout draws overlays-off (probed).
        if rv3d.view_perspective != "CAMERA":
            rv3d.view_perspective = "CAMERA"
        rv3d.view_camera_zoom = max(-30.0, min(600.0, state.cam_zoom + _zoom_offset))
        rv3d.view_camera_offset[0] = state.cam_offset[0]
        rv3d.view_camera_offset[1] = state.cam_offset[1]
    else:
        rows = [state.view_matrix[i : i + 4] for i in range(0, 16, 4)]
        rv3d.view_matrix = Matrix(rows)
        perspective = "PERSP" if state.is_persp else "ORTHO"
        if rv3d.view_perspective != perspective:
            rv3d.view_perspective = perspective
    space.lens = state.lens
    space.clip_start = state.clip_start
    space.clip_end = state.clip_end

    scene = bpy.context.scene
    now = time.monotonic()
    if (
        state.frame != scene.frame_current
        and now - _last_frame_apply >= _FRAME_MIN_INTERVAL
    ):
        scene.frame_set(state.frame)
        _last_frame_apply = now
    area.tag_redraw()


def notify_host_goodbye() -> None:
    """Called from the transport IO thread — flag only, no bpy."""
    global _host_goodbye
    _host_goodbye = True


def notify_new_session() -> None:
    """Fresh handshake: the new host session restarts its seq numbering, so
    the old tracker would count a false gap. IO thread — flag only."""
    global _new_session
    _new_session = True


def _handle_new_session() -> None:
    global _new_session, _seq_tracker, _reassembler
    _new_session = False
    _seq_tracker = protocol.SeqTracker()
    _reassembler = protocol.Reassembler()
    _blob_by_uuid.clear()
    stats["seq"] = 0
    stats["gaps"] = 0


def _handle_goodbye() -> None:
    """Host ended its session cleanly: idle the GPU (Cycles stops with the
    viewport out of RENDERED), restore a usable UI, stop the encoder. The
    replica keeps listening — the next host session bootstraps it straight
    back to work, no hands on this machine."""
    global _host_goodbye
    _host_goodbye = False
    import os, time
    if os.environ.get("QCB_DEBUG"):
        print(f"[{time.monotonic():.1f}] qcb GOODBYE handled", flush=True)
    area = _target_view()
    if area is not None:
        area.spaces.active.shading.type = "SOLID"
        area.tag_redraw()
    kiosk.exit_now()
    pixel_path.stop()
    stats["host_ended"] = True


def _tick():
    global _last_hot
    transport = _transport  # capture once: stop()/teardown can null the
    if transport is None:   # global mid-tick (seen as a quit-time traceback)
        return None  # unregister
    if _host_goodbye:
        _handle_goodbye()
    if _new_session:
        _handle_new_session()
    start = time.monotonic()
    packed = transport.poll_hot()
    if packed is not None and packed != _last_hot:
        _apply_hot(packed)
        _last_hot = packed
    if _pending_t2 is not None:
        _apply_pending_t2()  # indivisible, labeled — may blow the budget
    _process_cold(start + _BUDGET_S)
    area = _target_view()
    if area is not None and (_shot["on"] or _shot["restore"]):
        _service_shot(area)
    # A converged viewport stops redrawing, which would freeze the burned-in
    # overlay (incl. its latency clock). 10 Hz pokes are a cheap blit of the
    # finished render — Cycles sampling is untouched (stage-0 finding).
    global _last_redraw_poke
    if area is not None and start - _last_redraw_poke >= 0.1:
        _last_redraw_poke = start
        area.tag_redraw()
    kiosk.service(start)  # kiosk steps ride this tick — no timers of their own
    return _TICK


def start(transport, mappings=()) -> None:
    global _transport, _last_hot, _seq_tracker, _reassembler, _mappings, _pending_t2
    _transport = transport
    _last_hot = None
    _seq_tracker = protocol.SeqTracker()
    _reassembler = protocol.Reassembler()
    _inbox.clear()
    _blob_by_uuid.clear()
    _pending_t2 = None
    _mappings = list(mappings)
    stats.update(
        seq=0, gaps=0, applied_t1=0, applied_t2=0, apply_errors=0,
        unknown_uuid=0, bootstraps=0, unmapped_paths=0, applying="", last_error="",
    )
    _rebuild_uuid_map()
    # persistent: the apply loop must survive the bootstrap's open_mainfile
    # (non-persistent timers are dropped on file load)
    bpy.app.timers.register(_tick, first_interval=_TICK, persistent=True)


def stop() -> None:
    global _transport
    _transport = None
