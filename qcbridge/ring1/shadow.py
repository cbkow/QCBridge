"""Tier-1 tracked properties: tables + shadow store + diff.

Deliberately NOT a generic bpy.data mirror (decision #5): shadow copies exist
only for the tracked hot look-dev set. Ring0 reads bpy and hands plain
{rna_path: json-able value} snapshots in; this module only stores and diffs.

Static tables below cover fixed-path properties; node-socket paths are
dynamic (ring0's snapshot walker emits them per datablock) — the shadow
diffs whatever paths the snapshot contains, so both kinds flow identically.
A path that disappears from the snapshot (node deleted) is a structural
change; the classifier escalates those to tier 2 anyway.

Path prefixes with special meaning (ring0 owns both ends):
  "~"  structure signature — never sent, a change escalates to tier 2.
  "@"  non-RNA state applied by a dedicated setter (e.g. "@hide" = the
       per-view-layer eye toggle, hide_get()/hide_set() — not an RNA
       property at all); flows as a normal tier-1 change.
"""

from __future__ import annotations

# Fixed tracked paths by datablock type key (ring0 maps bpy types to these).
TRACKED: dict[str, tuple[str, ...]] = {
    "OBJECT": (
        "location",
        "rotation_euler",
        "rotation_quaternion",
        "rotation_mode",
        "scale",
        "color",
        "hide_viewport",
        "hide_render",
    ),
    "LIGHT": (
        "type",
        "color",
        "energy",
        "shadow_soft_size",
        "spot_size",
        "spot_blend",
    ),
    "CAMERA": (
        "lens",
        "clip_start",
        "clip_end",
        "sensor_width",
        "shift_x",
        "shift_y",
        "dof.use_dof",
        "dof.focus_distance",
        "dof.aperture_fstop",
        "show_passepartout",
        "passepartout_alpha",
    ),
    # Scene: color management (the replica bakes the SCENE's CM — stage-5
    # finding), render format (the one legitimate stream-restart trigger,
    # decision #12), viewport denoise (synced preference, 2026-07-24).
    "SCENE": (
        "render.engine",
        "render.resolution_x",
        "render.resolution_y",
        "render.resolution_percentage",
        "render.pixel_aspect_x",
        "render.pixel_aspect_y",
        "view_settings.view_transform",
        "view_settings.look",
        "view_settings.exposure",
        "view_settings.gamma",
        "cycles.use_preview_denoising",
        "cycles.preview_samples",
        "eevee.taa_samples",
    ),
    # MATERIAL / WORLD have no useful fixed paths — their snapshot is the
    # node-socket walk (dynamic paths) plus these:
    "MATERIAL": ("blend_method",),
    "WORLD": (),
    # Collections: the datablock-level toggles; the view-layer pair
    # (outliner checkbox/eye) rides as "@lc_exclude"/"@lc_hide", and
    # membership as a "~members" structure signature (ring0 emits all).
    "COLLECTION": ("hide_viewport", "hide_render"),
}


class ShadowStore:
    def __init__(self) -> None:
        self._shadows: dict[str, dict[str, object]] = {}

    def diff_and_update(
        self, uuid: str, snapshot: dict[str, object]
    ) -> dict | None:
        """Diff vs the shadow; updates the shadow.

        Returns None on first contact (no shadow yet — nothing to send: the
        peer got this state via tier-2/3, only *changes* ride tier 1).
        Otherwise {"changes": [(path, value)...], "structural": bool} —
        structural when the path set itself changed (node added/removed) or
        a "~" pseudo-path changed value (link rewiring, modifier stack):
        those cannot be expressed as property writes and must escalate to
        tier 2 (host_handlers does the escalation).
        """
        shadow = self._shadows.get(uuid)
        self._shadows[uuid] = dict(snapshot)
        if shadow is None:
            return None
        changes = [
            (path, value)
            for path, value in snapshot.items()
            if shadow.get(path, _MISSING) != value
        ]
        structural = shadow.keys() != snapshot.keys() or any(
            path.startswith("~") for path, _ in changes
        )
        return {
            "changes": [c for c in changes if not c[0].startswith("~")],
            "structural": structural,
        }

    def forget(self, uuid: str) -> None:
        self._shadows.pop(uuid, None)

    def known(self, uuid: str) -> bool:
        return uuid in self._shadows

    def __len__(self) -> int:
        return len(self._shadows)


class _Missing:
    __slots__ = ()

    def __eq__(self, other) -> bool:  # never equal to any value
        return False


_MISSING = _Missing()
