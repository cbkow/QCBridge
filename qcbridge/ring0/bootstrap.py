"""Tier-3: session bootstrap + force-resync (sync-protocol.md §Tier 3).

The only moments a full-reload cost is ever paid. Wire bytes only — the
Host save_copy's to its own scratch, ships chunked; the Replica opens its
local temp copy and fixes paths up. The addon never writes the project tree
(decision #16).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import bpy

from ..ring1 import pathmap
from . import kiosk


def serialize_mainfile(compress: bool = True) -> bytes:
    path = Path(tempfile.gettempdir()) / f"qcb-boot-{os.getpid()}.blend"
    try:
        # relative_remap=False is load-bearing: the default rewrites '//'
        # paths relative to the TEMP save location ('//../../..' climbs);
        # the wire form must stay relative to the PROJECT dir, which the
        # replica resolves against its mapped-local project dir.
        bpy.ops.wm.save_as_mainfile(
            filepath=str(path), copy=True, compress=compress, relative_remap=False
        )
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def project_dir_canonical(mappings) -> str:
    """The host project directory in wire form ('' if the file is unsaved)."""
    if not bpy.data.filepath:
        return ""
    return pathmap.to_canonical(os.path.dirname(bpy.data.filepath), mappings)


# Every bpy.data collection whose members carry a filepath worth localizing.
_PATH_COLLECTIONS = (
    "images", "libraries", "movieclips", "sounds", "fonts", "volumes",
    "cache_files",
)

_DEBUG = bool(os.environ.get("QCB_DEBUG"))


def _is_foreign_form(filepath: str) -> bool:
    if pathmap.current_os_tag() == "win":
        return filepath.startswith("/")   # mac-form absolute on a windows box
    return "\\" in filepath or (          # win-form absolute on a mac
        len(filepath) >= 2 and filepath[1] == ":" and filepath[0].isalpha()
    )


def localize_paths(datablocks, local_dir: str, mappings) -> tuple[int, int, int]:
    """Rewrite path-bearing datablocks for this machine. The replica works
    from a TEMP copy of the project, so '//' relative paths resolve against
    the wrong base — absolutize them against the mapped-local project dir;
    translate absolute foreign-form paths through the mapping table; skip
    packed data. Returns (fixed, unmapped, errors); unmapped paths surface
    in the burn-in overlay, never silently (decision #15)."""
    fixed = unmapped = errors = 0
    for db in datablocks:
        filepath = getattr(db, "filepath", None)
        if not filepath or getattr(db, "packed_file", None):
            continue
        new = None
        if filepath.startswith("//"):
            if local_dir:
                rel = filepath[2:].replace("\\", "/")
                new = os.path.normpath(os.path.join(local_dir, *rel.split("/")))
            else:
                unmapped += 1  # relative to a project dir this machine can't see
        else:
            local = pathmap.localize_any(filepath, mappings)
            if local != filepath:
                new = local
            elif _is_foreign_form(filepath) or not os.path.exists(bpy.path.abspath(filepath)):
                unmapped += 1  # no mapping matched and it is not here either
        if new and new != filepath:
            try:
                db.filepath = new
                if isinstance(db, bpy.types.Image):
                    db.reload()
                fixed += 1
                if _DEBUG:
                    print(f"qcb path {db.name}: {filepath!r} -> {new!r}", flush=True)
            except Exception as exc:
                errors += 1
                if _DEBUG:
                    print(f"qcb path FAIL {db.name}: {exc!r}", flush=True)
        elif _DEBUG and filepath.startswith("//") and not local_dir:
            print(f"qcb path SKIP {db.name}: relative but no project dir", flush=True)
    return fixed, unmapped, errors


def _iter_object_paths(obj):
    """(struct, attribute) pairs for the cache/bake paths a modifier keeps:
    external point caches, geometry-nodes bake directories, fluid caches.
    None of these is in a bpy.data path collection, so localize_paths never
    saw them (CACHES.md §4 D)."""
    for m in obj.modifiers:
        pc = getattr(m, "point_cache", None)
        if pc is not None and (pc.use_external or pc.filepath):
            yield pc, "filepath"
        canvas = getattr(m, "canvas_settings", None)
        if canvas is not None:
            for surf in canvas.canvas_surfaces:
                spc = surf.point_cache
                if spc is not None and (spc.use_external or spc.filepath):
                    yield spc, "filepath"
        if m.type == "NODES":
            yield m, "bake_directory"
            for bake in m.bakes:
                if bake.use_custom_path:
                    yield bake, "directory"
        if m.type == "FLUID" and getattr(m, "domain_settings", None) is not None:
            yield m.domain_settings, "cache_directory"
    for psys in obj.particle_systems:
        if psys.point_cache.use_external or psys.point_cache.filepath:
            yield psys.point_cache, "filepath"


def localize_object_paths(objects, local_dir: str, mappings) -> tuple[int, int, int]:
    """Same rule as localize_paths, for the paths that live on modifiers.
    Reassigning PointCache.filepath makes Blender rescan the directory and
    set is_baked from what is there (CACHES.md §2 finding 5) — but a rescan
    does not re-run the modifier, so if any cache was touched the current
    frame is re-applied: the replica is usually already sitting on the frame
    the host wants judged (measured 2026-09-23, run_smoke_cache)."""
    fixed = unmapped = errors = 0
    rescanned = False  # kept for readability of the branch above
    for obj in objects:
        try:
            pairs = list(_iter_object_paths(obj))
        except ReferenceError:
            continue
        for struct, attr in pairs:
            filepath = getattr(struct, attr, "") or ""
            if not filepath:
                continue
            new = None
            if filepath.startswith("//"):
                if local_dir:
                    rel = filepath[2:].replace("\\", "/")
                    new = os.path.normpath(os.path.join(local_dir, *rel.split("/")))
                else:
                    unmapped += 1
            else:
                local = pathmap.localize_any(filepath, mappings)
                if local != filepath:
                    new = local
                elif _is_foreign_form(filepath) or not os.path.isdir(filepath):
                    unmapped += 1
            try:
                if attr == "filepath":
                    # A point cache. An external cache with NO frames on disk
                    # is a hazard, not a cache: this replica would simulate
                    # into the shared directory and its writes poison the
                    # host's later bake (reproduced 2026-09-23). Keep it in
                    # memory until frames exist; the next settings resend
                    # (the host's bake flips is_baked) brings the path back,
                    # and then the reassignment rescans the real frames.
                    target = new or filepath
                    has_frames = os.path.isdir(target) and any(
                        f.endswith(".bphys") for f in os.listdir(target))
                    if not has_frames:
                        struct.use_external = False  # nothing there to delete
                        continue
                    # Frames exist. Assigning filepath (same or mapped) is
                    # the safe rescan: is_baked comes back, files stay, and
                    # every later seek reads them (probed). Re-setting
                    # use_disk_cache / use_external on an already-external,
                    # already-evaluated cache is what wiped 24 files to 2 —
                    # so external is switched on only when it is off (this
                    # replica switched it off above, earlier), and the disk
                    # flag is never touched here.
                    if not struct.use_external:
                        struct.use_external = True
                    setattr(struct, attr, target)
                    if new and new != filepath:
                        fixed += 1
                    continue
                if new and new != filepath:
                    setattr(struct, attr, new)
                    fixed += 1
            except Exception as exc:
                errors += 1
                if _DEBUG:
                    print(f"qcb path FAIL {obj.name}.{attr}: {exc!r}", flush=True)
    del rescanned  # no reseek: the next evaluation reads the frames as they are
    return fixed, unmapped, errors


def apply_mainfile(
    data: bytes, blob_tag: str, project_dir: str, mappings
) -> tuple[int, int, str]:
    """Open the shipped file and localize paths. Returns
    (errors, unmapped, local_project_dir) — the caller keeps the project dir
    for tier-2 arrivals, whose relative paths have the same wrong-base
    problem.

    Indivisible and heavy (the one legitimate full reload). App-level state
    (timers, draw handlers, transports) survives open_mainfile; scene-level
    state is rebuilt by the caller (uuid map, viewport prep).
    """
    path = Path(tempfile.gettempdir()) / f"qcb-in-{blob_tag}.blend"
    path.write_bytes(data)
    try:
        bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False)
    except Exception:
        path.unlink(missing_ok=True)
        return 1, 0, ""
    path.unlink(missing_ok=True)

    local_dir = pathmap.from_canonical(project_dir, mappings) if project_dir else ""
    if local_dir and not os.path.isdir(local_dir):
        # Unmapped or wrong: absolutizing '//' against it would fabricate
        # paths and count them as fixed. Leave them relative, count them.
        if _DEBUG:
            print(f"qcb project dir not found here: {local_dir!r}", flush=True)
        local_dir = ""
    errors = 0
    unmapped = 0
    for coll_name in _PATH_COLLECTIONS:
        _f, u, e = localize_paths(getattr(bpy.data, coll_name), local_dir, mappings)
        unmapped += u
        errors += e
    _f, u, e = localize_object_paths(bpy.data.objects, local_dir, mappings)
    unmapped += u
    errors += e
    # The scene's cycles.device arrived as the HOST set it (it's scene
    # state) — this machine is the GPU box, always render on the GPU.
    for scene in bpy.data.scenes:
        try:
            scene.cycles.device = "GPU"
        except AttributeError:
            pass  # cycles addon absent
    kiosk.prepare_viewport()
    kiosk.reassert()  # open_mainfile killed any in-flight kiosk steps
    return errors, unmapped, local_dir
