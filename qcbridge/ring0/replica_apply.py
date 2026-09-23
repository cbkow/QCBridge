"""Replica apply loop (sync-protocol.md §Replica apply loop).

bpy.app.timers tick ~15 ms, budget ~10 ms: drain the hot slot first, then pop
cold items until the budget is spent. frame_set is NOT a cheap write —
latest-wins absorbs scrub floods and a rate limit keeps scrubbing from
starving tier-1. All failures are counted and surfaced in the burn-in
overlay — the replica may be behind, never silently wrong.
"""

from __future__ import annotations

import collections
import os
import json
import time

import bpy
from mathutils import Matrix

from ..ring1 import merge, protocol
from . import bootstrap, kiosk, overlay, pixel_path, tier2_io
from .identity import UUID_PROP

_DEBUG = bool(os.environ.get("QCB_DEBUG"))

_TICK = 0.015
_BUDGET_S = 0.010
_FRAME_MIN_INTERVAL = 0.1

_SCANNED_COLLECTIONS = (
    "objects", "lights", "cameras", "materials", "worlds", "scenes", "meshes",
    "curves", "node_groups", "images", "collections", "actions", "shape_keys",
    "lattices", "armatures", "metaballs", "volumes", "hair_curves",
    "pointclouds", "lightprobes", "grease_pencils", "textures", "particles",
    "cache_files",
)

_transport = None
_last_hot: bytes | None = None
_last_hot_applied = 0.0
_REASSERT_S = 1.0
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
    # Raised on a seq gap or an unknown uuid, cleared when a bootstrap
    # applies; rides the pong so the host can ship one without a human.
    "want_resync": False,
    "seq_fast": 0,     # fast lane (tier-1 deltas, tombstones)
    "parked": 0,       # fast messages waiting for their blob to apply
    # Edits made ON the replica: nothing forbids them and the host will
    # never overwrite one unless it changes that same property (SYNC-AUDIT
    # A9). Heuristic count — an update on a stamped datablock we did not
    # touch recently, outside a frame change and outside a blob's wake.
    "local_edits": 0,
    "last_local_edit": "",
    # Baked disk caches with no external path: their blendcache_<name>/ dir
    # belongs to the host's file name, which this machine never had
    # (CACHES.md §2) — frozen at rest with every flag green.
    "frozen_caches": 0,
}

# Test hook: drop the first tier-1 message after start, to exercise the
# gap → want_resync → bootstrap path without a lossy network.
_TEST_DROP_FIRST_T1 = bool(os.environ.get("QCB_TEST_DROP_FIRST_T1"))

_merger = merge.LaneMerger()
_uuid_map: dict[str, bpy.types.ID] = {}
_reassembler = protocol.Reassembler()
_inbox: collections.deque = collections.deque()  # polled-but-unprocessed cold msgs
_inbox_fast: collections.deque = collections.deque()  # fast lane: never waits on a blob apply
_blob_by_uuid: dict[str, str] = {}   # uuid → latest in-flight blob id
_at_state: dict[str, dict[str, object]] = {}  # uuid → last "@" values applied
_touched: dict[str, float] = {}   # uuid → monotonic time we last wrote it
_quiet_until = 0.0                # no local-edit attribution before this (blob/boot/frame wake)
_TOUCH_GRACE = 1.5
_GEOMETRY_TYPES = (bpy.types.Mesh, bpy.types.Curve, bpy.types.Curves, bpy.types.PointCloud,
                   bpy.types.Volume, bpy.types.MetaBall, bpy.types.GreasePencil, bpy.types.Lattice)
_BLOB_GRACE = 3.0
_FRAME_GRACE = 0.5
_pending_t2_seq: int | None = None  # cold seq of the blob's last chunk
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
    # "@" paths: view-layer state and pointers applied by setter, not RNA
    # (shadow.py). Pointers ride as names: the replica's copy has the same
    # names, and tier-1 renames keep them in step.
    if path == "@hide":  # the object eye toggle
        db.hide_set(bool(value))
        return
    if path == "@scene_camera":
        db.camera = bpy.data.objects.get(value) if value else None
        return
    if path == "@scene_world":
        db.world = bpy.data.worlds.get(value) if value else None
        return
    if path == "@instance_collection":
        db.instance_collection = bpy.data.collections.get(value) if value else None
        return
    if path == "@ll_receiver":
        db.light_linking.receiver_collection = bpy.data.collections.get(value) if value else None
        return
    if path == "@ll_blocker":
        db.light_linking.blocker_collection = bpy.data.collections.get(value) if value else None
        return
    if path == "@dof_focus_object":
        db.dof.focus_object = bpy.data.objects.get(value) if value else None
        return
    if path == "@rbw":  # the rigid-body world exists (settings ride tier 1)
        if value and db.rigidbody_world is None:
            with bpy.context.temp_override(scene=db):
                bpy.ops.rigidbody.world_add()
        elif not value and db.rigidbody_world is not None:
            with bpy.context.temp_override(scene=db):
                bpy.ops.rigidbody.world_remove()
        return
    if path == "@master_members":
        # Direct membership of the scene's master collection: objects and
        # child collections, by name. Scene-embedded, so no tier 2 carries it.
        want_objs, want_colls = set(value[0]), set(value[1])
        master = db.collection
        for o in list(master.objects):
            if o.name not in want_objs:
                master.objects.unlink(o)
        for name in want_objs:
            o = bpy.data.objects.get(name)
            if o is not None and o.name not in master.objects:
                master.objects.link(o)
        for c in list(master.children):
            if c.name not in want_colls:
                master.children.unlink(c)
        for name in want_colls:
            c = bpy.data.collections.get(name)
            if c is not None and c.name not in master.children:
                master.children.link(c)
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


def _note_touched(uuid: str) -> None:
    _touched[uuid] = time.monotonic()


def _quiet(seconds: float) -> None:
    global _quiet_until
    _quiet_until = max(_quiet_until, time.monotonic() + seconds)


def _apply_t1(header: dict, payload: bytes) -> None:
    db = _resolve(header["uuid"])
    _note_touched(header["uuid"])
    # A delta on one datablock wakes its dependents (a camera's data → its
    # object); the detector judges only while the host has been quiet.
    _quiet(_FRAME_GRACE)
    if db is None:
        stats["unknown_uuid"] += 1
        stats["want_resync"] = True  # a blob we never got
        return
    changes = json.loads(payload.decode("utf-8"))
    for path, value in changes:
        try:
            _write_rna(db, path, value)
            stats["applied_t1"] += 1
            if path.startswith("@"):
                # View-layer / pointer state a later blob will not carry and
                # the host will not resend (its shadow already has it):
                # keep it to re-apply after apply_blob (SYNC-AUDIT A5).
                _at_state.setdefault(header["uuid"], {})[path] = value
        except Exception as exc:
            stats["apply_errors"] += 1
            stats["last_error"] = f"{header['uuid']}.{path}: {exc!r}"


def _apply_tombstone(header: dict) -> None:
    global _last_hot
    _note_touched(header["uuid"])
    _quiet(_FRAME_GRACE)  # a removal wakes the survivors
    db = _resolve(header["uuid"])
    if db is None:
        return
    try:
        bpy.data.batch_remove((db,))
    except Exception:
        stats["apply_errors"] += 1
    _uuid_map.pop(header["uuid"], None)
    _last_hot = None  # same camera-view risk as a t2 apply (see above)


def _apply_link(header: dict) -> None:
    """Link (or drop) datablocks from another .blend, by the replica's
    mapped path. Linked IDs are never stamped, so membership and pointers
    to them ride by name once they exist here."""
    from ..ring1 import pathmap
    _quiet(_BLOB_GRACE)  # libraries.load re-evaluates the scene: not a local edit
    host_path = header.get("lib") or ""
    local = pathmap.localize_any(host_path, _mappings)
    if header.get("kind") == "unlink":
        for lib in list(bpy.data.libraries):
            if bpy.path.abspath(lib.filepath) in (local, host_path):
                bpy.data.libraries.remove(lib)
        return
    if not os.path.exists(local):
        stats["unmapped_paths"] += 1
        stats["last_error"] = f"library not found here: {local}"
        return
    want_objects = set(header.get("objects") or [])
    want_colls = set(header.get("collections") or [])
    have = {o.name for o in bpy.data.objects if o.library and bpy.path.abspath(o.library.filepath) == local}
    have_c = {c.name for c in bpy.data.collections if c.library and bpy.path.abspath(c.library.filepath) == local}
    try:
        with bpy.data.libraries.load(local, link=True) as (data_from, data_to):
            data_to.objects = [n for n in want_objects - have if n in data_from.objects]
            data_to.collections = [n for n in want_colls - have_c if n in data_from.collections]
        stats["applied_t2"] += 1
    except Exception as exc:
        stats["apply_errors"] += 1
        stats["last_error"] = f"link {os.path.basename(local)}: {exc!r}"
    _rebuild_uuid_map()


def _reapply_at_state(uuid: str) -> None:
    """After a blob replaced a datablock, put back the "@" state the host
    sent earlier: the blob does not carry it, and the host's shadow already
    holds it so it will never be resent."""
    saved = _at_state.get(uuid)
    if not saved:
        return
    db = _resolve(uuid)
    if db is None:
        return
    for path, value in saved.items():
        try:
            _write_rna(db, path, value)
        except Exception as exc:
            stats["apply_errors"] += 1
            stats["last_error"] = f"{uuid}.{path} (re-apply): {exc!r}"


def _point_caches():
    for obj in bpy.data.objects:
        for m in obj.modifiers:
            pc = getattr(m, "point_cache", None)
            if pc is not None:
                yield pc
            canvas = getattr(m, "canvas_settings", None)
            if canvas is not None:
                for surf in canvas.canvas_surfaces:
                    if surf.point_cache is not None:
                        yield surf.point_cache
        for psys in obj.particle_systems:
            yield psys.point_cache
    for scene in bpy.data.scenes:
        rbw = scene.rigidbody_world
        if rbw is not None and rbw.point_cache is not None:
            yield rbw.point_cache


def _count_frozen_caches() -> int:
    """Baked disk caches with no external path. `is_baked` is serialized
    state, not a filesystem check: the frames are in a blendcache_ dir named
    after the HOST's file, which does not exist here, and the flag stops
    Blender from simulating either (probed 2026-09-23, CACHES.md §2)."""
    n = 0
    try:
        # Blender keeps a non-external disk cache in //blendcache_<stem>/;
        # check that directory, not the flag.
        fp = bpy.data.filepath
        stem = os.path.splitext(os.path.basename(fp))[0] if fp else ""
        cache_dir = os.path.join(os.path.dirname(fp), f"blendcache_{stem}") if fp else ""
        have_dir = bool(cache_dir) and os.path.isdir(cache_dir)
        for pc in _point_caches():
            if pc.use_disk_cache and not pc.use_external and pc.is_baked and not have_dir:
                n += 1
    except Exception:
        pass  # mid-apply states are fine to skip
    return n


def _apply_pending_t2() -> None:
    global _pending_t2, _project_dir_local, _last_hot
    header, blob = _pending_t2
    _pending_t2 = None
    blob_tag = header["blob"]["id"].replace(".", "-")
    _note_touched(header.get("uuid", ""))
    _quiet(_BLOB_GRACE)  # a blob (and its dependencies) wakes the depsgraph broadly
    try:
        if header.get("kind") == "boot":
            errors, unmapped, _project_dir_local = bootstrap.apply_mainfile(
                blob, blob_tag, header.get("project_dir", ""), _mappings
            )
            stats["bootstraps"] += 1
            stats["apply_errors"] += errors
            stats["unmapped_paths"] = unmapped
            stats["host_ended"] = False
            stats["want_resync"] = False  # the full file is the answer
            from . import session  # deferred: session imports this module
            session.on_project_loaded()
        else:
            _name, errors = tier2_io.apply_blob(
                blob, blob_tag, _mappings, _project_dir_local
            )
            stats["applied_t2"] += 1
            stats["apply_errors"] += errors
        _rebuild_uuid_map()
        if header.get("kind") == "boot":
            _at_state.clear()  # the file carries view-layer state itself
        else:
            _reapply_at_state(header.get("uuid", ""))
        stats["frozen_caches"] = _count_frozen_caches()
    except Exception as exc:
        stats["apply_errors"] += 1
        stats["last_error"] = f"{header.get('kind')} {header.get('name')}: {exc!r}"
    finally:
        stats["applying"] = ""
        _release(_merger.cold_done(_pending_t2_seq))
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
    # One message at a time through the inboxes so a mid-batch stop (a
    # completed tier-2 blob deferring its apply) never drops already-polled
    # messages. Fast messages are applied even while a blob apply is
    # pending: the merge rule parks any that must follow it (LaneMerger),
    # so the rest are independent of it by construction.
    global _pending_t2
    transport = _transport
    if transport is None:
        return
    while time.monotonic() < deadline:
        if not _inbox_fast and hasattr(transport, "poll_fast"):
            _inbox_fast.extend(transport.poll_fast(64))
        if _inbox_fast:
            header, payload = _inbox_fast.popleft()
        elif _pending_t2 is None:
            if not _inbox:
                _inbox.extend(transport.poll_cold(8))
                if not _inbox:
                    return
            header, payload = _inbox.popleft()
        else:
            return  # cold waits for the pending apply; fast is drained
        seq = header.get("seq")
        kind = header.get("kind")
        global _TEST_DROP_FIRST_T1
        if _TEST_DROP_FIRST_T1 and kind == "t1":
            _TEST_DROP_FIRST_T1 = False
            continue  # simulate a lost frame: the next seq reveals the gap
        if not _merger.observe(header):
            stats["want_resync"] = True
        stats["seq"] = _merger.cold.last_seen or 0
        stats["seq_fast"] = _merger.fast.last_seen or 0
        stats["gaps"] = _merger.gaps
        if not _merger.admit(header, payload):
            stats["parked"] = _merger.parked
            continue  # fast message waiting for the blob it must follow
        _dispatch(header, payload, seq)


def _dispatch(header: dict, payload: bytes, seq) -> None:
    global _pending_t2, _pending_t2_seq
    kind = header.get("kind")
    if _merger.lane_of(header) == merge.LANE_FAST:
        if kind == "t1":
            _apply_t1(header, payload)
        elif kind == "tomb":
            _apply_tombstone(header)
        return
    if kind == "t1":
        _apply_t1(header, payload)
        _release(_merger.cold_done(seq))
    elif kind == "tomb":
        _apply_tombstone(header)
        _release(_merger.cold_done(seq))
    elif kind in ("link", "unlink"):
        _apply_link(header)
        _release(_merger.cold_done(seq))
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
                _pending_t2_seq = seq  # cold_done only once it has applied
                area = _target_view()
                if area:
                    area.tag_redraw()
            else:
                _release(_merger.cold_done(seq))  # a middle chunk: nothing waits on it


def _release(items: list) -> None:
    """Parked fast messages whose blob has now applied."""
    for header, payload in items:
        _dispatch(header, payload, header.get("seq"))
    stats["parked"] = _merger.parked


def _target_view():
    """The largest 3D viewport — mirror of host_hot's pick, so a multi-area
    replica layout follows in the viewport the operator actually watches."""
    best = None
    best_size = -1
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                size = area.width * area.height
                if size > best_size:
                    best, best_size = area, size
    return best


def _camera_view_bound(rv3d, cam) -> bool:
    """Is the region genuinely looking through cam? In camera view the view
    matrix is the camera's inverted (orthonormal) world matrix; zoom/offset
    are 2D projection params and don't enter it."""
    expect = cam.matrix_world.normalized().inverted()
    return all(
        abs(a - b) <= 1e-3
        for row_have, row_want in zip(rv3d.view_matrix, expect)
        for a, b in zip(row_have, row_want)
    )


def _apply_hot(packed: bytes, reassert: bool = False) -> None:
    _quiet(_FRAME_GRACE)
    global _last_frame_apply
    state = protocol.unpack_hot(packed)
    if state is None:
        return
    area = _target_view()
    if area is None:
        return
    space = area.spaces.active
    rv3d = space.region_3d
    if rv3d is None:
        return  # region mid-rebuild (kiosk maximize/fullscreen transition)
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
    # Compare-before-write throughout: this runs on the 1 s re-assert loop
    # with a usually-correct view, and a view3d RNA write restarts Cycles
    # viewport sampling even when the value is identical — the converged
    # beauty must stay converged (the 10 Hz redraw poke is a blit precisely
    # because of that).
    changed = False
    if state.camera:
        # Camera view: the camera object defines the view — setting the
        # matrix would fight it. Zoom = host's zoom + the remote nudge
        # offset, so the replica can be punched in/out independently while
        # still following. Passepartout draws overlays-off (probed).
        cam = bpy.context.scene.camera
        if cam is None:
            # Nothing to look through (pre-bootstrap scene): writing the
            # enum now would set "CAMERA" with no camera bound — a state
            # the != guard then treats as done forever. Field symptom:
            # replica starts outside camera view until manually toggled
            # out-and-in (Num0 twice). Leave the view; the 1 s re-assert
            # retries after the bootstrap lands a scene camera.
            pass
        elif rv3d.view_perspective != "CAMERA":
            rv3d.view_perspective = "CAMERA"
            changed = True
        elif reassert and not _camera_view_bound(rv3d, cam):
            # Enum says CAMERA but the region isn't looking through the
            # scene camera (the write landed during a kiosk relayout or
            # against a camera-less scene). The manual fix was Num0
            # out+in — do exactly that, atomically within this tick.
            # Only on the re-assert path: while packets differ (host
            # navigating) a transient mismatch is normal, not desync.
            rv3d.view_perspective = "PERSP"
            rv3d.view_perspective = "CAMERA"
            changed = True
        zoom = max(-30.0, min(600.0, state.cam_zoom + _zoom_offset))
        if abs(rv3d.view_camera_zoom - zoom) > 1e-6:
            rv3d.view_camera_zoom = zoom
            changed = True
        if (
            abs(rv3d.view_camera_offset[0] - state.cam_offset[0]) > 1e-6
            or abs(rv3d.view_camera_offset[1] - state.cam_offset[1]) > 1e-6
        ):
            rv3d.view_camera_offset[0] = state.cam_offset[0]
            rv3d.view_camera_offset[1] = state.cam_offset[1]
            changed = True
    else:
        rows = [state.view_matrix[i : i + 4] for i in range(0, 16, 4)]
        matrix = Matrix(rows)
        # readback can differ in the last bits from what was written —
        # tolerance compare, or the guard would never hold
        if any(
            abs(a - b) > 1e-6
            for ra, rb in zip(rv3d.view_matrix, matrix)
            for a, b in zip(ra, rb)
        ):
            rv3d.view_matrix = matrix
            changed = True
        perspective = "PERSP" if state.is_persp else "ORTHO"
        if rv3d.view_perspective != perspective:
            rv3d.view_perspective = perspective
            changed = True
    for attr, value in (
        ("lens", state.lens), ("clip_start", state.clip_start),
        ("clip_end", state.clip_end),
    ):
        if abs(getattr(space, attr) - value) > 1e-6:
            setattr(space, attr, value)
            changed = True

    scene = bpy.context.scene
    now = time.monotonic()
    if (
        state.frame != scene.frame_current
        and now - _last_frame_apply >= _FRAME_MIN_INTERVAL
    ):
        scene.frame_set(state.frame)
        _last_frame_apply = now
        changed = True
    if changed:
        area.tag_redraw()


def notify_host_goodbye() -> None:
    """Called from the transport IO thread — flag only, no bpy."""
    global _host_goodbye
    _host_goodbye = True


_new_session_seqs = (0, 0)


def notify_new_session(seq_cold: int = 0, seq_fast: int = 0) -> None:
    """Fresh handshake: the host's lane counters are wherever it left them,
    so the trackers prime to what the hello reports — otherwise the first
    message on a lane is a fresh start and a lost first message is no gap.
    IO thread — flag only."""
    global _new_session, _new_session_seqs
    _new_session_seqs = (int(seq_cold or 0), int(seq_fast or 0))
    _new_session = True


def _handle_new_session() -> None:
    global _new_session, _reassembler
    _new_session = False
    _merger.reset()
    # Keep zeros: 0 means "the next message is seq 1", not "unknown" — a
    # lane the host never used before a restart must still show its gaps.
    _merger.cold.last_seen = _new_session_seqs[0]
    _merger.fast.last_seen = _new_session_seqs[1]
    _merger.cold_applied = _new_session_seqs[0]
    _reassembler = protocol.Reassembler()
    _blob_by_uuid.clear()
    _at_state.clear()
    stats["seq"] = 0
    stats["seq_fast"] = 0
    stats["parked"] = 0
    stats["gaps"] = 0
    stats["want_resync"] = False


def _handle_goodbye() -> None:
    """Host ended its session cleanly: idle the GPU (Cycles stops with the
    viewport out of RENDERED), restore a usable UI, stop the encoder. The
    replica keeps listening — the next host session bootstraps it straight
    back to work, no hands on this machine."""
    global _host_goodbye, _last_hot
    _host_goodbye = False
    _last_hot = None  # stop the periodic re-assert: no session to follow
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
    transport = _transport  # capture once: stop()/teardown can null the
    if transport is None:   # global mid-tick (seen as a quit-time traceback)
        return None  # unregister
    # One exception must cost one tick, not the replica: an unguarded raise
    # (e.g. a space dying mid-kiosk-relayout) would unregister this timer —
    # hot, cold, kiosk and shot all silently stop while the host looks
    # connected. Same hardening the host flush timer has.
    try:
        return _tick_inner(transport)
    except Exception as exc:
        stats["apply_errors"] += 1
        stats["last_error"] = f"tick: {exc!r}"
        return _TICK


def _tick_inner(transport):
    global _last_hot, _last_hot_applied
    if _host_goodbye:
        _handle_goodbye()
    if _new_session:
        _handle_new_session()
    start = time.monotonic()
    packed = transport.poll_hot()
    if packed is not None:
        stamped = protocol.unpack_hot(packed)
        if stamped is not None and stamped.t_host:
            # Parity probe: the strip must show the newest stamp even on a
            # static view, so it redraws on every stamped packet.
            overlay.set_probe(stamped.t_host, stamped.probe_seq)
            probe_area = _target_view()
            if probe_area is not None:
                probe_area.tag_redraw()
        packed = protocol.hot_core(packed)
    if packed is not None and packed != _last_hot:
        _apply_hot(packed)
        _last_hot = packed
        _last_hot_applied = start
    elif _last_hot is not None and start - _last_hot_applied >= _REASSERT_S:
        # Self-healing follow (kiosk-verify pattern): a kiosk relayout, a
        # manual view nudge on this machine, or an early packet landing
        # before the bootstrap's scene camera existed all leave the view
        # wrong while a STATIC host emits byte-identical packets — which
        # the dedup above rightly skips. Re-assert the last state on a 1 s
        # cadence; the writes are idempotent, so a correct view is
        # untouched and a disturbed one converges within a second.
        _apply_hot(_last_hot, reassert=True)
        _last_hot_applied = start
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


@bpy.app.handlers.persistent
def _on_depsgraph_replica(scene, depsgraph) -> None:
    """Count edits this replica made itself. Attribution is by exclusion:
    a stamped datablock we did not write in the last 1.5 s, updated outside
    the 3 s after a blob/bootstrap and the 0.5 s after a frame change."""
    if _transport is None:
        return
    now = time.monotonic()
    if now < _quiet_until:
        return
    for update in depsgraph.updates:
        db = update.id.original
        if isinstance(db, (bpy.types.Scene, bpy.types.WindowManager, bpy.types.Screen)):
            continue
        uuid = db.get(UUID_PROP) if hasattr(db, "get") else None
        if not uuid:
            continue
        if now - _touched.get(uuid, float("-inf")) < _TOUCH_GRACE:
            continue
        if (
            update.is_updated_shading
            and not update.is_updated_geometry
            and not update.is_updated_transform
            and isinstance(db, (bpy.types.Object,) + _GEOMETRY_TYPES)
        ):
            # A shading-only update on an object or its geometry is a
            # consequence of something else changing (a material, a light,
            # a link); a local edit of the object shows as transform or
            # geometry. A local material edit still shows on the Material.
            continue
        if _DEBUG:
            print(f"qcb local-edit? {type(db).__name__}:{db.name} geom={update.is_updated_geometry}"
                  f" shade={update.is_updated_shading} xform={update.is_updated_transform}", flush=True)
        stats["local_edits"] += 1
        stats["last_local_edit"] = f"{type(db).__name__}:{db.name}"
        return  # one per batch is enough to raise the flag


def start(transport, mappings=()) -> None:
    global _transport, _last_hot, _last_hot_applied, \
        _reassembler, _mappings, _pending_t2
    _transport = transport
    _last_hot = None
    _last_hot_applied = 0.0
    _merger.reset()
    _reassembler = protocol.Reassembler()
    _inbox.clear()
    _inbox_fast.clear()
    _blob_by_uuid.clear()
    _at_state.clear()
    _pending_t2 = None
    _mappings = list(mappings)
    stats.update(
        seq=0, gaps=0, applied_t1=0, applied_t2=0, apply_errors=0,
        unknown_uuid=0, bootstraps=0, unmapped_paths=0, applying="", last_error="",
        want_resync=False, frozen_caches=0, seq_fast=0, parked=0,
    )
    _rebuild_uuid_map()
    _touched.clear()
    stats.update(local_edits=0, last_local_edit="")
    _quiet(_BLOB_GRACE)
    if _on_depsgraph_replica not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_replica)
    # persistent: the apply loop must survive the bootstrap's open_mainfile
    # (non-persistent timers are dropped on file load)
    bpy.app.timers.register(_tick, first_interval=_TICK, persistent=True)


def stop() -> None:
    global _transport
    _transport = None
    if _on_depsgraph_replica in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_replica)
