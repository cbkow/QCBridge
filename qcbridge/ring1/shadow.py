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
    # Paths absent on a variant (spot props on a sun, area size on a point)
    # raise on path_resolve and are skipped per datablock — list generously.
    # Pointer-valued state (instance collection, focus object, scene world)
    # cannot be a path: it rides as an "@" setter emitted by ring0.
    "OBJECT": (
        "name",
        "location",
        "rotation_euler",
        "rotation_quaternion",
        "rotation_mode",
        "scale",
        "delta_location",
        "delta_rotation_euler",
        "delta_scale",
        "color",
        "hide_viewport",
        "hide_render",
        "display_type",
        "show_in_front",
        "show_wire",
        "show_name",
        "show_axis",
        "visible_camera",
        "visible_diffuse",
        "visible_glossy",
        "visible_transmission",
        "visible_volume_scatter",
        "visible_shadow",
        "is_holdout",
        "is_shadow_catcher",
        "instance_type",
        "use_instance_vertices_rotation",
        "empty_display_type",
        "empty_display_size",
        "empty_image_offset",
        "empty_image_depth",
        "field.type",
        "field.strength",
        "field.flow",
        "field.noise",
        "field.seed",
        "field.shape",
        "field.falloff_type",
        "field.falloff_power",
        "field.use_max_distance",
        "field.distance_max",
        "field.use_min_distance",
        "field.distance_min",
        "field.wind_factor",
        "field.inflow",
    ),
    "LIGHT": (
        "name",
        "type",
        "color",
        "energy",
        "exposure",
        "normalize",
        "shadow_soft_size",
        "use_shadow",
        "spot_size",
        "spot_blend",
        "show_cone",
        "shape",
        "size",
        "size_y",
        "spread",
        "angle",
        "diffuse_factor",
        "specular_factor",
        "volume_factor",
        "transmission_factor",
        "use_custom_distance",
        "cutoff_distance",
        "shadow_jitter_overblur",
    ),
    "CAMERA": (
        "name",
        "type",
        "lens",
        "lens_unit",
        "ortho_scale",
        "clip_start",
        "clip_end",
        "sensor_width",
        "sensor_height",
        "sensor_fit",
        "shift_x",
        "shift_y",
        "dof.use_dof",
        "dof.focus_distance",
        "dof.aperture_fstop",
        "dof.aperture_blades",
        "dof.aperture_rotation",
        "dof.aperture_ratio",
        "show_passepartout",
        "passepartout_alpha",
        "show_background_images",
        "display_size",
    ),
    # Scene: color management (the replica bakes the SCENE's CM — stage-5
    # finding), render format (the one legitimate stream-restart trigger,
    # decision #12), viewport denoise (synced preference, 2026-07-24).
    # Scene has no tier 2 (it is the whole file), so its scalars must all be
    # here; pointers (world, camera) and structure (markers, view layers,
    # compositor, master-collection membership) ride "@"/"~" from ring0.
    "SCENE": (
        "name",
        "frame_start",
        "frame_end",
        "frame_step",
        "render.fps",
        "render.fps_base",
        "render.engine",
        "render.resolution_x",
        "render.resolution_y",
        "render.resolution_percentage",
        "render.pixel_aspect_x",
        "render.pixel_aspect_y",
        "render.film_transparent",
        "render.use_motion_blur",
        "render.motion_blur_shutter",
        "render.use_border",
        "render.use_crop_to_border",
        "render.border_min_x",
        "render.border_max_x",
        "render.border_min_y",
        "render.border_max_y",
        "render.filter_size",
        "render.use_simplify",
        "render.simplify_subdivision",
        "view_settings.view_transform",
        "view_settings.look",
        "view_settings.exposure",
        "view_settings.gamma",
        "display_settings.display_device",
        "unit_settings.system",
        "unit_settings.scale_length",
        "unit_settings.length_unit",
        "unit_settings.system_rotation",
        "gravity",
        "use_gravity",
        "cycles.use_preview_denoising",
        "cycles.preview_samples",
        "cycles.samples",
        "cycles.use_denoising",
        "cycles.use_adaptive_sampling",
        "cycles.adaptive_threshold",
        "cycles.time_limit",
        "cycles.max_bounces",
        "cycles.film_exposure",
        "eevee.taa_samples",
        "eevee.taa_render_samples",
        "eevee.use_shadows",
        "eevee.use_raytracing",
        "eevee.use_volumetric_shadows",
        "rigidbody_world.enabled",
        "rigidbody_world.time_scale",
        "rigidbody_world.substeps_per_frame",
        "rigidbody_world.solver_iterations",
        "rigidbody_world.use_split_impulse",
    ),
    # MATERIAL / WORLD: the node-socket walk (dynamic paths) and the node
    # property signature ("~nodes", ring0) carry the tree; these are the
    # datablock-level settings.
    "MATERIAL": (
        "name",
        "blend_method",
        "surface_render_method",
        "use_backface_culling",
        "use_backface_culling_shadow",
        "use_transparent_shadow",
        "displacement_method",
        "diffuse_color",
        "metallic",
        "roughness",
        "specular_intensity",
        "pass_index",
        "use_nodes",
    ),
    "WORLD": ("name", "use_nodes", "color"),
    # Collections: the datablock-level toggles; the view-layer pair
    # (outliner checkbox/eye) rides as "@lc_exclude"/"@lc_hide", and
    # membership as a "~members" structure signature (ring0 emits all).
    "COLLECTION": ("name", "hide_viewport", "hide_render", "hide_select",
                   "instance_offset", "color_tag"),
    # Shape keys: all paths are dynamic (key_blocks["Name"].value/.mute —
    # ring0 emits them per block); block add/remove/rename changes the path
    # set and escalates structurally like node edits.
    "KEY": ("name",),
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
