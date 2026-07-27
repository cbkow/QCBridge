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


def serialize_mainfile() -> bytes:
    path = Path(tempfile.gettempdir()) / f"qcb-boot-{os.getpid()}.blend"
    try:
        # relative_remap=False is load-bearing: the default rewrites '//'
        # paths relative to the TEMP save location ('//../../..' climbs);
        # the wire form must stay relative to the PROJECT dir, which the
        # replica resolves against its mapped-local project dir.
        bpy.ops.wm.save_as_mainfile(
            filepath=str(path), copy=True, compress=True, relative_remap=False
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
            local = pathmap.from_canonical(filepath, mappings)
            if local != filepath:
                new = local
            elif _is_foreign_form(filepath):
                unmapped += 1
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
    errors = 0
    unmapped = 0
    for coll_name in _PATH_COLLECTIONS:
        _f, u, e = localize_paths(getattr(bpy.data, coll_name), local_dir, mappings)
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
