"""Tier-2 datablock resend: serialize via Blender's own format, apply via
user_remap (sync-protocol.md §Tier 2 — the workhorse fallback).

Serialization is bpy.data.libraries.write of ONE datablock: 100% fidelity by
construction, dependencies included, format-stable. The replica appends
everything the blob contains, remaps every uuid-stamped arrival over its
stale predecessor, deletes the stale copies, and restores names. Incoming
dependencies always win (the blob is current host state — replace-by-recency).
Cycles rebuilds only the affected BLAS; the session never reloads.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import bpy

_DEBUG = bool(os.environ.get("QCB_DEBUG"))

from . import bootstrap
from .identity import UUID_PROP

# ID class → bpy.data collection name (serialize + apply both need it).
_COLLECTIONS = (
    ("objects", bpy.types.Object),
    ("meshes", bpy.types.Mesh),
    ("curves", bpy.types.Curve),
    ("lights", bpy.types.Light),
    ("cameras", bpy.types.Camera),
    ("materials", bpy.types.Material),
    ("worlds", bpy.types.World),
    ("images", bpy.types.Image),
    ("node_groups", bpy.types.NodeTree),
    ("collections", bpy.types.Collection),
    # Actions must be listed for PAIRING, not just transport: an action
    # riding a blob as an unlisted dependency would append as a .001
    # duplicate instead of remapping over its stale predecessor.
    ("actions", bpy.types.Action),
    ("lattices", bpy.types.Lattice),
    ("armatures", bpy.types.Armature),
    # NOTE no shape_keys entry: libraries.load has no shape_keys namespace
    # (probed 5.2) — Keys always ride their owner's blob and are paired by
    # the dedicated post-pass in apply_blob below.
)


def collection_of(db: bpy.types.ID) -> str | None:
    for coll_name, bpy_type in _COLLECTIONS:
        if isinstance(db, bpy_type):
            return coll_name
    return None


def serialize(db: bpy.types.ID) -> bytes | None:
    """Partial .blend bytes for one datablock (deps included), or None for
    types we don't resend (Scene: force-resync territory, M6)."""
    if collection_of(db) is None:
        return None
    path = Path(tempfile.gettempdir()) / f"qcb-t2-{db.session_uid}.blend"
    try:
        bpy.data.libraries.write(str(path), {db}, compress=True)
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def apply_blob(
    data: bytes, blob_tag: str, mappings, local_project_dir: str = ""
) -> tuple[str, int]:
    """Append-load a tier-2/3 partial blend and swap it in.

    Returns (primary_name, errors). Indivisible by design — the caller labels
    it in the burn-in overlay while it runs.
    """
    path = Path(tempfile.gettempdir()) / f"qcb-in-{blob_tag}.blend"
    path.write_bytes(data)
    errors = 0
    loaded: list[tuple[bpy.types.ID, str]] = []  # (new_db, intended_name)
    try:
        with bpy.data.libraries.load(str(path)) as (data_from, data_to):
            intended: dict[str, list[str]] = {}
            for coll_name, _ in _COLLECTIONS:
                # str() copies matter: Blender mutates the assigned list IN
                # PLACE during load, replacing names with the loaded IDs —
                # keep our own strings or "intended names" become IDs.
                names = [str(n) for n in getattr(data_from, coll_name)]
                if names:
                    setattr(data_to, coll_name, list(names))
                    intended[coll_name] = names
        for coll_name, _ in _COLLECTIONS:
            for new_db, name in zip(
                getattr(data_to, coll_name), intended.get(coll_name, ())
            ):
                if new_db is not None:
                    loaded.append((new_db, name))
    finally:
        path.unlink(missing_ok=True)

    # Pass 1: pair arrivals with stale predecessors — by uuid first, falling
    # back to (collection, name) for predecessors that carry no stamp (a
    # pre-bootstrap replica copy; production bootstrap ships fully-stamped
    # files, but the apply must not tangle names when the contract is loose).
    arrivals = {db.as_pointer() for db, _ in loaded}
    pairs = []
    for new_db, intended_name in loaded:
        uuid = new_db.get(UUID_PROP)
        old_db = _find_by_uuid(uuid, exclude=new_db) if uuid else None
        if old_db is None:
            # plain iteration: bpy_prop_collection.get() has raised
            # SystemError here on freshly-appended state (observed 5.2)
            for candidate in getattr(bpy.data, collection_of(new_db)):
                if (
                    candidate.name == intended_name
                    and candidate.as_pointer() not in arrivals
                ):
                    old_db = candidate
                    break
        pairs.append((new_db, old_db, intended_name))
        if _DEBUG:
            print(
                f"qcb pair {collection_of(new_db)}: new={new_db.name!r}"
                f" intended={intended_name!r}"
                f" old={old_db.name if old_db else None!r}"
                f" uuid={uuid!r}",
                flush=True,
            )
    # Pass 2: remap all references old → new.
    for new_db, old_db, _ in pairs:
        if old_db is not None:
            try:
                old_db.user_remap(new_db)
            except Exception as exc:
                if _DEBUG:
                    print(f"qcb remap FAIL {new_db.name}: {exc!r}", flush=True)
                errors += 1
    # Pass 3: delete stale copies, then restore names (frees .001 suffixes).
    stale = tuple(old for _, old, _ in pairs if old is not None)
    if stale:
        try:
            bpy.data.batch_remove(stale)
        except Exception as exc:
            if _DEBUG:
                print(f"qcb batch_remove FAIL: {exc!r}", flush=True)
            errors += 1
    primary_name = ""
    for new_db, _, intended_name in pairs:
        if new_db.name != intended_name:
            try:
                new_db.name = intended_name
            except Exception:
                errors += 1
        primary_name = primary_name or new_db.name
    # Shape-key post-pass: a Key arrives implicitly with its owner (it
    # cannot be listed in data_to — no shape_keys namespace, probed 5.2) as
    # a "<name>.001" copy, while the replica's previous Key is orphaned by
    # the stale owner's removal above. Pair by stamp, take over the old
    # name, drop the orphan — without this every resend of a keyed mesh
    # leaks a duplicate Key.
    for new_db, _, _ in pairs:
        new_key = getattr(new_db, "shape_keys", None)
        if new_key is None:
            continue
        uuid = new_key.get(UUID_PROP)
        old_key = None
        if uuid:
            for candidate in bpy.data.shape_keys:
                if candidate is not new_key and candidate.get(UUID_PROP) == uuid:
                    old_key = candidate
                    break
        if old_key is not None:
            try:
                old_name = old_key.name
                old_key.user_remap(new_key)
                bpy.data.batch_remove((old_key,))
                new_key.name = old_name
            except Exception:
                errors += 1
    # The replica works from a temp copy of the project (tier-3), so arrived
    # relative paths need the same localize pass as bootstrap.
    _f, _u, path_errors = bootstrap.localize_paths(
        [db for db, _, _ in pairs], local_project_dir, mappings
    )
    errors += path_errors
    # New objects aren't linked into any scene collection by append-into-data;
    # link the ones that ended up orphaned.
    for new_db, old_db, _ in pairs:
        if isinstance(new_db, bpy.types.Object) and not new_db.users_collection:
            bpy.context.scene.collection.objects.link(new_db)
    return primary_name, errors


def _find_by_uuid(uuid: str, exclude: bpy.types.ID):
    for coll_name, _ in _COLLECTIONS:
        for db in getattr(bpy.data, coll_name):
            if db is not exclude and db.get(UUID_PROP) == uuid:
                return db
    return None
