"""Tier-3: session bootstrap + force-resync (sync-protocol.md §Tier 3).

The only moments a full-reload cost is ever paid. Wire bytes only — the
Host save_copy's to its own scratch, ships chunked; the Replica opens its
local temp copy and fixes paths up. The addon never writes the project tree
(decision #16).
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import bpy

from ..ring1 import pathmap, timeline
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

# Replica: where each geometry-nodes modifier's disk bake is on THIS machine,
# by (object identity, modifier name). A tier-2 resend of the object arrives
# with an EMPTY bake_directory (libraries.write drops the relative folder -
# probed 2026-11-19), and an object with bake nodes is resent on every bare
# geometry update, so without this the bake was lost again moments after the
# project loaded. Refilled at every bootstrap.
_bake_dirs: dict[tuple[str, str], str] = {}


def _bake_key(obj, modifier) -> tuple[str, str]:
    return (str(obj.get("qcbridge_uuid") or obj.name), modifier.name)


last_unmapped: list[str] = []   # "Type:name:path" of what the last apply_mainfile could not place


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
        if not filepath or getattr(db, "packed_file", None) or filepath.startswith("<"):
            continue  # empty, packed, or Blender's own "<builtin>" font
        if isinstance(db, bpy.types.Library) and "/datafiles/assets/" in filepath.replace("\\", "/"):
            # Blender's own bundled asset libraries (Essentials: the dynamics,
            # hair and geometry-nodes assets). The stored path is the install
            # folder of whichever machine saved the file; every Blender finds
            # its own copy at load. Rewriting it against the project folder
            # made a path that exists nowhere and a failed reload - six errors
            # on every load of a file using them (found 2026-11-19).
            continue
        new = None
        if filepath.startswith("//"):
            if local_dir:
                rel = filepath[2:].replace("\\", "/")
                new = os.path.normpath(os.path.join(local_dir, *rel.split("/")))
            else:
                unmapped += 1  # relative to a project dir this machine can't see
                last_unmapped.append(f"{type(db).__name__}:{db.name}:{filepath}")
        else:
            local = pathmap.localize_any(filepath, mappings)
            if local != filepath:
                new = local
            elif _is_foreign_form(filepath) or not os.path.exists(bpy.path.abspath(filepath)):
                unmapped += 1  # no mapping matched and it is not here either
                last_unmapped.append(f"{type(db).__name__}:{db.name}:{filepath}")
        if new and new != filepath:
            try:
                db.filepath = new
                if isinstance(db, bpy.types.Image):
                    db.reload()
                elif isinstance(db, bpy.types.Library):
                    db.reload()  # a repointed library is placeholders until reloaded
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
    saw them (DESIGN-NOTES caches §4 D)."""
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
    set is_baked from what is there (DESIGN-NOTES caches §2 finding 5) — but a rescan
    does not re-run the modifier, so if any cache was touched the current
    frame is re-applied: the replica is usually already sitting on the frame
    the host wants judged (measured 2026-09-23, run_smoke_cache)."""
    fixed = unmapped = errors = 0
    rescanned = False  # kept for readability of the branch above
    rebaked = []       # geometry-nodes modifiers whose bake folder was repointed
    for obj in objects:
        try:
            # an arrival that lost its bake folder gets back the one this machine had for it
            for m in obj.modifiers:
                if m.type == "NODES" and len(m.bakes) and not m.bake_directory and m.bake_target == "DISK":
                    kept = _bake_dirs.get(_bake_key(obj, m))
                    if kept and os.path.isdir(kept):
                        m.bake_directory = kept
                        fixed += 1
                        rebaked.append(m)
                        timeline.mark("bake.dir_restored", object=obj.name[:40], modifier=m.name[:40])
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
                    if attr in ("bake_directory", "directory"):
                        owner = struct if attr == "bake_directory" else next(
                            (m for m in obj.modifiers if m.type == "NODES" and any(b == struct for b in m.bakes)), None)
                        if owner is not None and owner not in rebaked:
                            rebaked.append(owner)
            except Exception as exc:
                errors += 1
                if _DEBUG:
                    print(f"qcb path FAIL {obj.name}.{attr}: {exc!r}", flush=True)
    # A bake folder repointed AFTER the file was loaded is not read: Blender
    # 5.3 looked for the bake when it first evaluated the modifier (against
    # the temp copy's own folder, where there is none) and keeps simulating
    # live until its simulation state is reset - by a visit to the start
    # frame, or by this: the node group taken off and put back. Probed
    # 2026-11-19 on the owner's cloth (a simulation zone inside a linked
    # group, disk bake, relative folder): assigning the path alone, tagging
    # the object, toggling the modifier, the bake target and a same-value
    # group assignment all leave it unbaked; off-and-on reads the bake at
    # once and keeps the modifier's inputs and bake ids.
    for obj in objects:
        try:
            for m in obj.modifiers:
                if m.type == "NODES" and len(m.bakes) and m.bake_directory and not m.bake_directory.startswith("//"):
                    key = _bake_key(obj, m)
                    if _bake_dirs.get(key) != m.bake_directory:
                        # (said once per folder: what this machine found there - the answer to
                        # "why is the cache not used here")
                        found = os.path.isdir(m.bake_directory)
                        timeline.mark("bake.dir", object=obj.name[:40], modifier=m.name[:40], found=found,
                                      files=(sum(len(f) for _, _, f in os.walk(m.bake_directory)) if found else 0))
                    _bake_dirs[key] = m.bake_directory
        except ReferenceError:
            continue
    for m in rebaked:
        try:
            group = m.node_group
            if group is not None:
                m.node_group = None
                m.node_group = group
        except Exception as exc:
            errors += 1
            if _DEBUG:
                print(f"qcb bake reset FAIL {m.name}: {exc!r}", flush=True)
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
    last_unmapped.clear()
    _bake_dirs.clear()
    t_open = time.monotonic()
    try:
        bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False)
    except Exception as exc:
        path.unlink(missing_ok=True)
        timeline.mark("boot.open_mainfile.failed", exc=repr(exc)[:160])
        return 1, 0, ""
    path.unlink(missing_ok=True)
    timeline.mark("boot.open_mainfile", s=time.monotonic() - t_open, bytes=len(data),
                  libraries=len(bpy.data.libraries), images=len(bpy.data.images), objects=len(bpy.data.objects))

    local_dir = pathmap.from_canonical(project_dir, mappings) if project_dir else ""
    if local_dir and not os.path.isdir(local_dir):
        # Unmapped or wrong: absolutizing '//' against it would fabricate
        # paths and count them as fixed. Leave them relative, count them.
        if _DEBUG:
            print(f"qcb project dir not found here: {local_dir!r}", flush=True)
        timeline.mark("project_dir.missing", local=local_dir, canonical=project_dir)
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
    # state) — this machine is the GPU box, always render on the GPU. And
    # the host's Cycles is our subclass here, at 1:1 viewport pixels
    # (DESIGN-NOTES pixels).
    from . import engine_qcb
    for scene in bpy.data.scenes:
        try:
            scene.cycles.device = "GPU"
        except AttributeError:
            pass  # cycles addon absent
        engine_qcb.adopt_scene(scene)
    kiosk.prepare_viewport()
    kiosk.reassert()  # open_mainfile killed any in-flight kiosk steps
    return errors, unmapped, local_dir
