"""UUID stamping on datablocks (sync-protocol.md: "design this first").

The Host stamps a uuid4 hex as an id custom property at first contact; the
wire protocol keys on these, never on names — renames become tier-1 deltas
and `user_remap` targets resolve reliably.

Notes bound to Blender behavior:
- Writing an id property dirties the datablock and can retrigger the
  depsgraph handler; the handler's re-entrancy guard lives in
  host_handlers.py (M4), not here.
- Duplicating a datablock copies custom properties — ensure_uuid() resolves
  the collision by restamping the newcomer (the registry, keyed on Blender's
  session_uid, tells the two apart).
- Library-linked datablocks are read-only and are NOT stamped (decision #15
  scope guard: no linked-library machinery); callers skip them.
"""

from __future__ import annotations

import uuid as _uuid

import bpy

from ..ring1.registry import IdentityRegistry

UUID_PROP = "qcbridge_uuid"


def is_stampable(datablock: bpy.types.ID) -> bool:
    return datablock.library is None and not datablock.is_embedded_data


def ensure_uuid(datablock: bpy.types.ID, registry: IdentityRegistry) -> str:
    """Return the datablock's stable uuid, stamping or restamping as needed."""
    stamped = datablock.get(UUID_PROP, "")
    if stamped and registry.claim(stamped, datablock.session_uid) == "ok":
        return stamped
    # First contact (no stamp) or a duplicate carrying a copied stamp.
    fresh = _uuid.uuid4().hex
    datablock[UUID_PROP] = fresh
    registry.claim(fresh, datablock.session_uid)
    return fresh


def read_uuid(datablock: bpy.types.ID) -> str | None:
    """The stamp as-is, no side effects (Replica side: stamps arrive already
    written by the Host inside tier-2/tier-3 payloads)."""
    return datablock.get(UUID_PROP) or None
