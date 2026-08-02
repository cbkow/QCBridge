"""Host change detection: depsgraph → classifier → dirty set → cold channel.

Flow (docs/architecture.md §Host process model): depsgraph_update_post marks
datablocks in the debouncer + dirty set; a flush timer drains debounced
uuids, diffs tracked snapshots against the shadow, and sends tier-1 tuples.
Structural/unrecognized changes ratchet to tier-2 in the dirty set (sent by
M5's machinery; until then they sit there, honestly counted).

Pause (auto-with-pause): the flush timer simply doesn't drain while paused —
the dirty set and debouncer keep absorbing, bounded by scene size.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import bpy
from bpy.app.handlers import persistent
import idprop
import mathutils

from ..ring1 import protocol
from ..ring1.classifier import Debouncer, classify_update
from ..ring1.dirtyset import DirtySet, Tier
from ..ring1.registry import IdentityRegistry
from ..ring1.shadow import TRACKED, ShadowStore
from . import bootstrap, identity, tier2_io

_FLUSH_TICK = 0.05
_SWEEP_INTERVAL = 0.5
_DEBUG = bool(os.environ.get("QCB_DEBUG"))

# bpy ID type → shadow.TRACKED key (isinstance covers subclasses, e.g. the
# per-kind Light types). Types outside this map are tier-2 only.
_TYPE_KEYS = {
    bpy.types.Object: "OBJECT",
    bpy.types.Light: "LIGHT",
    bpy.types.Camera: "CAMERA",
    bpy.types.Scene: "SCENE",
    bpy.types.Material: "MATERIAL",
    bpy.types.World: "WORLD",
    bpy.types.Collection: "COLLECTION",
    bpy.types.Key: "KEY",
}


def _layer_collections():
    """collection session_uid → LayerCollection, for the active view layer."""
    result = {}

    def walk(lc):
        result[lc.collection.session_uid] = lc
        for child in lc.children:
            walk(child)

    walk(bpy.context.view_layer.layer_collection)
    return result

# Collections swept for deletions (only types we stamp/track).
_SWEPT_COLLECTIONS = (
    "objects", "lights", "cameras", "materials", "worlds", "scenes", "meshes",
    "curves", "images", "node_groups", "collections", "actions", "shape_keys",
    "lattices", "armatures",
)


class HostSync:
    def __init__(self, transport, paused_fn, mappings=()) -> None:
        self.transport = transport
        self.paused_fn = paused_fn
        self.mappings = list(mappings)
        self._boot_outbox: list[tuple[dict, bytes]] = []
        self.sent_boot = 0
        self.registry = IdentityRegistry()
        self.shadow = ShadowStore()
        self.debounce = Debouncer()
        self.dirty = DirtySet()
        self.seq = 0
        self.sent_t1 = 0
        self.sent_t2 = 0
        self.t2_unsupported = 0
        self.bake_note = ""  # a sim bake appeared/vanished: only tier 3
                             # carries cache data (probed 5.2) — panel nags
                             # for Force Resync until one ships
        self._uuid_to_db: dict[str, bpy.types.ID] = {}
        self._vis_state: dict[str, tuple] = {}  # uuid → visibility vector
        self._last_sweep = 0.0
        self._in_handler = False
        self._initial_scan()

    def _sweep_visibility(self) -> None:
        """Visibility changes don't reliably produce depsgraph events: the
        eye toggle (hide_set/H) is view-layer state, not a datablock
        property, and even hide_viewport fires nothing for the object it
        disables (measured 5.2 — plausibly because the object leaves the
        depsgraph). Sample all three flags on the sweep cadence and mark
        tier-1 dirty on change; the normal flush diffs and ships them.

        Point-cache state rides the same sweep for the same reason: a bake
        finishing (job thread) or Delete Bake gives no dependable event for
        the owning object; the flush diff sees "~pcache" change and
        escalates to tier 2 so the baked cache travels in the resend."""
        now = time.monotonic()
        for obj in bpy.data.objects:
            uuid = self.registry.uuid_for(obj.session_uid)
            if uuid is None:
                continue
            vector = _object_sweep_vector(obj)
            old = self._vis_state.get(uuid)
            if old != vector:
                if old is not None and len(old) >= 4:
                    # A bake appearing/disappearing needs tier 3: the tier-2
                    # partial blend does NOT contain cache data (probed —
                    # only the full save does). Settings still resend via
                    # the "~pcache" escalation; the bake itself waits on a
                    # manual Force Resync (resync stays manual, decision #8).
                    old_on = {row[0] for row in old[3] if row[1]}
                    new_on = {row[0] for row in vector[3] if row[1]}
                    if old_on != new_on:
                        self.bake_note = f"sim bake changed ({obj.name})"
                self._vis_state[uuid] = vector
                self.dirty.mark(uuid, Tier.T1)
                self.debounce.touch(uuid, now)
        layer_collections = _layer_collections()
        for coll in bpy.data.collections:
            uuid = self.registry.uuid_for(coll.session_uid)
            if uuid is None:
                continue
            lc = layer_collections.get(coll.session_uid)
            vector = (
                lc.exclude if lc else None,
                lc.hide_viewport if lc else None,
                coll.hide_viewport,
                coll.hide_render,
            )
            if self._vis_state.get(uuid) != vector:
                self._vis_state[uuid] = vector
                self.dirty.mark(uuid, Tier.T1)
                self.debounce.touch(uuid, now)

    def reset_for_new_file(self) -> None:
        """The host opened a different file (or a new one) mid-session: all
        prior identity/shadow state refers to dead datablocks. Rebuild from
        the new file and reship it — the replica mirrors whatever the host
        has open (owner's call, 2026-07-25). Seq stays monotonic; a pending
        boot outbox is superseded wholesale."""
        self.registry = IdentityRegistry()
        self.shadow = ShadowStore()
        self.debounce = Debouncer()
        self.dirty = DirtySet()
        self._uuid_to_db = {}
        self._boot_outbox.clear()
        self._initial_scan()
        self.send_bootstrap()

    def _initial_scan(self) -> None:
        """Stamp + register + shadow-prime every syncable datablock at session
        start. Without this, a never-edited datablock is invisible to the
        registry and its deletion would go unnoticed. Priming the shadow here
        is correct by the bootstrap contract: both ends start from identical
        state, so only *changes* from now on ride tier 1."""
        for coll_name in _SWEPT_COLLECTIONS:
            for db in getattr(bpy.data, coll_name):
                if not identity.is_stampable(db):
                    continue
                uuid = identity.ensure_uuid(db, self.registry)
                self._uuid_to_db[uuid] = db
                self.dirty.assume_known(uuid)
                if _type_key(db) is not None:
                    snapshot = build_snapshot(db)
                    self.shadow.diff_and_update(uuid, snapshot)
                    if isinstance(db, bpy.types.Object):
                        self._vis_state[uuid] = _object_sweep_vector(db)

    # ── depsgraph handler (main thread) ──────────────────────────────────────

    def on_depsgraph(self, scene, depsgraph) -> None:
        if self._in_handler:
            return
        self._in_handler = True
        try:
            now = time.monotonic()
            for update in depsgraph.updates:
                db = update.id.original
                if not identity.is_stampable(db):
                    continue
                type_key = _type_key(db)
                if type_key is None and not _is_syncable_id(db):
                    continue
                uuid = identity.ensure_uuid(db, self.registry)
                self._uuid_to_db[uuid] = db
                tier = classify_update(type_key, update.is_updated_geometry)
                if _DEBUG:
                    print(
                        f"qcb classify {type(db).__name__}:{db.name} -> T{int(tier)}"
                        f" (geom={update.is_updated_geometry}"
                        f" shade={update.is_updated_shading}"
                        f" xform={update.is_updated_transform})",
                        flush=True,
                    )
                self.dirty.mark(uuid, tier)
                self.debounce.touch(uuid, now)
        finally:
            self._in_handler = False

    # ── flush timer (main thread) ────────────────────────────────────────────

    def send_bootstrap(self) -> None:
        """Queue the full mainfile for the wire (tier 3 — session bootstrap
        and force-resync). Chunks drain through flush_tick under
        backpressure; explicit, so it proceeds even while paused."""
        data = bootstrap.serialize_mainfile()
        meta = {
            "uuid": "__mainfile__",
            "name": bpy.path.basename(bpy.data.filepath) or "untitled",
            "project_dir": bootstrap.project_dir_canonical(self.mappings),
        }
        blob_id = f"boot.{self.sent_boot}.{self.seq + 1}"
        self._boot_outbox.extend(protocol.chunk_blob("boot", blob_id, data, meta=meta))
        self.sent_boot += 1
        self.bake_note = ""  # the full file carries every baked cache
        if _DEBUG:
            print(f"qcb boot queued ({len(data)} bytes)", flush=True)

    def flush_tick(self) -> float:
        now = time.monotonic()
        while self._boot_outbox:
            header, payload = self._boot_outbox[0]
            self.seq += 1
            header["seq"] = self.seq
            if self.transport.send_cold(header, payload):
                self._boot_outbox.pop(0)
            else:
                self.seq -= 1
                return _FLUSH_TICK  # backpressure: try again next tick
        if now - self._last_sweep >= _SWEEP_INTERVAL:
            self._sweep_deletions()
            self._last_sweep = now
        if self.paused_fn() or not len(self.dirty):
            return _FLUSH_TICK
        ready_set = set(self.debounce.ready(now))
        tombstones, dirty = self.dirty.drain()
        for uuid in tombstones:
            self._send_tombstone(uuid)
        for uuid, tier in dirty:
            if uuid not in ready_set:
                self.dirty.requeue(uuid, tier)  # still being edited
                continue
            if tier == Tier.T2:
                self._flush_t2(uuid)
            else:
                self._flush_t1(uuid)
        return _FLUSH_TICK

    def _flush_t1(self, uuid: str) -> None:
        db = self._uuid_to_db.get(uuid)
        if db is None:
            return
        try:
            snapshot = build_snapshot(db)
        except ReferenceError:  # died between mark and flush; sweep will see it
            return
        diff = self.shadow.diff_and_update(uuid, snapshot)
        if diff is None:
            # First contact AFTER the initial scan (which primes every
            # scan-time shadow): a datablock created mid-session, which the
            # peer has never seen — "nothing to diff" must mean "send it
            # whole", not silence. Found live: a new camera + rail linked
            # straight into the scene MASTER collection synced nothing —
            # master-collection membership is embedded in the Scene and
            # rides no collection datablock's "~members" resend.
            self._flush_t2(uuid)
            return
        if diff["structural"]:
            # Node add/remove, link rewiring, modifier-stack change — not
            # expressible as property writes; escalate (decision #5: the
            # replica may be behind, never wrong). Flush now: this uuid is
            # already debounce-ready, and no further event may ever touch it.
            if _DEBUG:
                print(f"qcb escalate T2 {db.name}", flush=True)
            self._flush_t2(uuid)
            return
        changes = diff["changes"]
        if not changes:
            return
        self.seq += 1
        header = {"kind": "t1", "seq": self.seq, "uuid": uuid}
        payload = json.dumps(changes).encode("utf-8")
        if self.transport.send_cold(header, payload):
            self.sent_t1 += len(changes)
            if _DEBUG:
                print(f"qcb t1 send {db.name}: {[p for p, _ in changes]}", flush=True)
        else:
            self.seq -= 1
            self.dirty.requeue(uuid, Tier.T1)

    def _flush_t2(self, uuid: str) -> None:
        db = self._uuid_to_db.get(uuid)
        if db is None:
            return
        if isinstance(db, bpy.types.Key):
            # A Key can't travel alone — libraries.load exposes no
            # shape_keys namespace (probed 5.2); the owner's blob carries
            # it. Refresh the Key's shadow first so tier-1 doesn't re-diff
            # the same structural change forever.
            self.shadow.diff_and_update(uuid, build_snapshot(db))
            owner = db.user
            if owner is None:
                return
            owner_uuid = identity.ensure_uuid(owner, self.registry)
            self._uuid_to_db[owner_uuid] = owner
            if owner_uuid != uuid:
                self._flush_t2(owner_uuid)
            return
        try:
            data = tier2_io.serialize(db)
        except ReferenceError:
            return  # died between mark and flush; sweep will tombstone it
        if data is None:
            # Type we don't resend (e.g. Scene structural change) — surfaced,
            # not silent: force-resync (M6) is the recovery.
            self.t2_unsupported += 1
            if _DEBUG:
                print(f"qcb t2 UNSUPPORTED {type(db).__name__}:{db.name}", flush=True)
            return
        # Refresh the shadow so tier-1 doesn't re-send state the blob carries.
        self.shadow.diff_and_update(uuid, build_snapshot(db))
        blob_id = f"{uuid}.{self.seq + 1}"
        meta = {"uuid": uuid, "name": db.name, "coll": tier2_io.collection_of(db)}
        for header, payload in protocol.chunk_blob("t2", blob_id, data, meta=meta):
            self.seq += 1
            header["seq"] = self.seq
            if not self.transport.send_cold(header, payload):
                self.seq -= 1
                self.dirty.requeue(uuid, Tier.T2)  # partial superseded later
                return
        self.sent_t2 += 1
        if _DEBUG:
            print(f"qcb t2 send {db.name} ({len(data)} bytes)", flush=True)

    def _send_tombstone(self, uuid: str) -> None:
        self.seq += 1
        if not self.transport.send_cold({"kind": "tomb", "seq": self.seq, "uuid": uuid}):
            self.seq -= 1
            self.dirty.requeue_tombstone(uuid)

    def _sweep_deletions(self) -> None:
        self._sweep_visibility()
        live = set()
        for coll_name in _SWEPT_COLLECTIONS:
            for db in getattr(bpy.data, coll_name):
                live.add(db.session_uid)
        dead = [
            uuid
            for uuid, db in list(self._uuid_to_db.items())
            if self.registry.session_uid_for(uuid) not in live
        ]
        for uuid in dead:
            self.dirty.mark_deleted(uuid)
            self.debounce.drop(uuid)
            self.shadow.forget(uuid)
            self.registry.forget_uuid(uuid)
            self._uuid_to_db.pop(uuid, None)


# ── snapshots (bpy reads → plain json-able dicts) ────────────────────────────

def _type_key(db: bpy.types.ID) -> str | None:
    for bpy_type, key in _TYPE_KEYS.items():
        if isinstance(db, bpy_type):
            return key
    return None


def _is_syncable_id(db: bpy.types.ID) -> bool:
    # Data-level ids that tier-2 will resend wholesale (meshes, curves, node
    # groups, images…): stamp + mark, no tier-1 table. Actions belong here:
    # keyframe edits fire on the Action ID (probed 5.2) — without this, a
    # retimed rig animation never crossed, and an object-key edit HALF-synced
    # (the current-frame pose rode tier 1, then the replica's stale action
    # snapped it back on the next scrub).
    return isinstance(
        db, (bpy.types.Mesh, bpy.types.Curve, bpy.types.NodeTree,
             bpy.types.Action, bpy.types.Lattice, bpy.types.Armature)
    )


def _coerce(value):
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (mathutils.Vector, mathutils.Euler, mathutils.Quaternion, mathutils.Color)):
        return list(value)
    if isinstance(value, (idprop.types.IDPropertyArray, tuple, list)):
        return [_coerce(v) for v in value]
    try:  # bpy_prop_array and friends
        return [_coerce(v) for v in value]
    except TypeError:
        return None


# Identity/state props excluded from settings digests: identity rides the
# signature columns next to the digest; point-cache state has its own
# "~pcache" signature (and its info string churns during plain playback).
_DIGEST_SKIP = {"rna_type", "name", "type", "show_expanded", "is_active", "point_cache"}


def _digest_value(value):
    if isinstance(value, bpy.types.ID):
        return ("id", value.session_uid)  # identity, rename-proof
    if isinstance(value, set):  # enum-flag props iterate unordered
        return tuple(sorted(value))
    return _round5(_coerce(value))


def _round5(value):
    if isinstance(value, float):
        return round(value, 5)
    if isinstance(value, list):
        return [_round5(v) for v in value]
    return value


def _settings_digest(struct, depth: int = 0) -> str:
    """Stable hash of a modifier's/constraint's settings, one value per RNA
    property, descending into non-ID sub-structs (ClothSettings and friends)
    and recording ID pointers by identity. Folded into the "~modifiers" /
    "~constraints" signatures so PROPERTY edits — cloth stiffness, a Track
    To's influence, a boolean's target — escalate to a tier-2 resend
    (previously only stack shape did; field report 2026-08-01)."""
    parts: list = []
    for prop in struct.bl_rna.properties:
        ident = prop.identifier
        if ident in _DIGEST_SKIP:
            continue
        if prop.type == "POINTER":
            try:
                value = getattr(struct, ident)
            except AttributeError:
                continue
            if value is None:
                parts.append((ident, None))
            elif isinstance(value, bpy.types.ID):
                parts.append((ident, value.session_uid))
            elif depth < 2:
                parts.append((ident, _settings_digest(value, depth + 1)))
        elif prop.type in {"BOOLEAN", "INT", "FLOAT", "ENUM", "STRING"}:
            if prop.is_readonly:
                continue  # derived state (is_bound, …), not a setting
            try:
                value = getattr(struct, ident)
            except AttributeError:
                continue
            parts.append((ident, _digest_value(value)))
    if depth == 0:
        try:
            parts.append(("_idprops", [
                (key, _digest_value(val)) for key, val in struct.items()
            ]))
        except TypeError:
            pass  # modifiers don't support classic IDProperties on 5.2
        if isinstance(struct, bpy.types.NodesModifier):
            parts.append(("_gn_inputs", _gn_input_values(struct)))
    return hashlib.md5(repr(parts).encode()).hexdigest()[:16]


def _gn_input_values(mod) -> list:
    """Geometry Nodes modifier input values, 5.2 storage: NOT classic
    idprops (items() raises) and NOT RNA — each socket is an IDPropertyGroup
    behind mod.properties.inputs[<socket identifier>]. Probed: a panel input
    tweak fires only the OBJECT's depsgraph update, so unless these values
    reach the digest, the edit syncs nothing. Tree-side edits fire on the
    GeometryNodeTree ID and resend through the existing tier-2 path."""
    if mod.node_group is None:
        return []
    values = []
    try:
        inputs = mod.properties.inputs
        for item in mod.node_group.interface.items_tree:
            ident = getattr(item, "identifier", "")
            if getattr(item, "in_out", None) != "INPUT" or not ident:
                continue
            try:
                group = inputs[ident]
            except KeyError:
                continue
            raw = group.to_dict() if hasattr(group, "to_dict") else None
            if isinstance(raw, dict):
                values.append((ident, sorted(
                    (key, _digest_value(val)) for key, val in raw.items()
                )))
    except (AttributeError, TypeError):
        pass  # storage shape differs on this Blender; tree edits still resend
    return values


_BBONE_PROPS = (
    "bbone_curveinx", "bbone_curveinz", "bbone_curveoutx", "bbone_curveoutz",
    "bbone_easein", "bbone_easeout", "bbone_rollin", "bbone_rollout",
    "bbone_scalein", "bbone_scaleout",
)


def _pose_digest(pose, full: bool) -> str:
    """Armature pose as one hash. full=True (flush snapshot) covers
    everything the tier-2 object blob would change; full=False is the sweep
    variant — transforms + bone idprops only, cheap enough for 0.5 s cadence
    and exactly the edits that fire no depsgraph event (bone rig sliders).
    Constraint/bbone edits DO fire (probed), so the flush digest catches
    them without sweep help."""
    parts = []
    for pb in pose.bones:
        entry = [
            pb.name,
            [round(v, 5) for row in pb.matrix_basis for v in row],
            [(k, _digest_value(v)) for k, v in sorted(
                pb.items(), key=lambda kv: kv[0])],
        ]
        if full:
            entry.append(pb.rotation_mode)
            entry.append([_coerce(getattr(pb, n, None)) for n in _BBONE_PROPS])
            entry.append(
                [[c.name, c.type, _settings_digest(c)] for c in pb.constraints]
            )
        parts.append(entry)
    return hashlib.md5(repr(parts).encode()).hexdigest()[:16]


def _idprops_digest(struct) -> str:
    parts = [
        (k, _digest_value(v))
        for k, v in sorted(struct.items(), key=lambda kv: kv[0])
        if k != identity.UUID_PROP and not k.startswith("_")
    ]
    return hashlib.md5(repr(parts).encode()).hexdigest()[:16]


def _object_sweep_vector(obj: bpy.types.Object) -> tuple:
    """Everything sampled on the sweep because no reliable depsgraph event
    exists for it: visibility (original case), point-cache state, custom
    properties (raw idprop writes fire nothing — probed), and the light
    pose digest for armatures (bone idprop sliders, same silence)."""
    try:
        eye = obj.hide_get()
    except RuntimeError:
        eye = None  # not in the active view layer
    return (
        eye, obj.hide_viewport, obj.hide_render,
        _pcache_signature(obj),
        _idprops_digest(obj),
        _pose_digest(obj.pose, full=False) if obj.pose is not None else None,
    )


def _anim_signature(anim) -> list | None:
    """Animation linkage on one ID: which action, NLA shape, and the driver
    set. Values inside the action are NOT here — those edits fire on the
    Action ID and resend through tier 2 like any datablock."""
    if anim is None:
        return None
    drivers = [
        (
            fc.data_path, fc.array_index, fc.driver.type,
            fc.driver.expression,
            [
                (var.name, var.type, [
                    (t.id.session_uid if t.id else None, t.data_path)
                    for t in var.targets
                ])
                for var in fc.driver.variables
            ],
        )
        for fc in anim.drivers
    ]
    return [
        anim.action.session_uid if anim.action else None,
        len(anim.nla_tracks),
        drivers,
    ]


def _pcache_signature(obj: bpy.types.Object) -> list:
    """Point-cache state per sim on this object (cloth, soft body, particles,
    dynamic paint surfaces). Bake/Delete-Bake changes exactly these fields —
    deliberately NOT the cache info string, which changes on every frame of
    plain playback caching and would turn scrubbing into a resend storm.
    Unbaked live sims stay out of scope: only a baked cache is state the
    resend can faithfully carry."""
    sig = []

    def add(tag, pc):
        if pc is not None:
            sig.append([
                tag, pc.is_baked, pc.use_disk_cache, pc.frame_start, pc.frame_end
            ])

    for m in obj.modifiers:
        add(m.name, getattr(m, "point_cache", None))
        canvas = getattr(m, "canvas_settings", None)
        if canvas is not None:
            for surf in canvas.canvas_surfaces:
                add(f"{m.name}/{surf.name}", getattr(surf, "point_cache", None))
    for psys in obj.particle_systems:
        add(f"psys/{psys.name}", psys.point_cache)
    return sig


def build_snapshot(db: bpy.types.ID) -> dict:
    """Tracked values plus "~" pseudo-paths: structure signatures that are
    never sent as tier-1 writes — a change in one escalates to tier 2."""
    snapshot: dict[str, object] = {}
    if hasattr(db, "animation_data"):
        # Action ASSIGNMENT and driver changes fire only on the owning ID
        # (probed 5.2) — signature them so they escalate; the tier-2 blob
        # carries animation_data and the action rides as a dependency.
        # Keyframe edits inside an existing action fire on the Action ID
        # itself, which is now a syncable tier-2 type of its own.
        snapshot["~anim"] = _anim_signature(db.animation_data)
    type_key = _type_key(db)
    for path in TRACKED.get(type_key, ()):
        try:
            value = _coerce(db.path_resolve(path))
        except ValueError:
            continue  # path absent on this variant (e.g. spot props on a sun)
        if value is not None:
            snapshot[path] = value
    if isinstance(db, bpy.types.Object):
        snapshot["~modifiers"] = [
            [m.name, m.type, m.show_viewport, _settings_digest(m)]
            for m in db.modifiers
        ]
        # Parenting is invisible to the tier-1 table: Ctrl+P stores the kept
        # offset in matrix_parent_inverse and leaves loc/rot/scale alone, so
        # the diff saw nothing (field report 2026-08-01). Parent identity is
        # the session_uid — rename-proof; the wire never sees it (a change
        # escalates to tier 2, where libraries.write carries the parent as a
        # dependency and the replica remaps by stamped uuid).
        snapshot["~parent"] = None if db.parent is None else [
            db.parent.session_uid,
            db.parent_type,
            db.parent_bone,
            list(db.parent_vertices),
            [round(v, 6) for row in db.matrix_parent_inverse for v in row],
        ]
        # Constraints (camera rigs: Track To on a null, etc.) — the digest
        # covers targets, influence and per-type settings, so tweaks resend,
        # not just stack add/remove.
        snapshot["~constraints"] = [
            [c.name, c.type, _settings_digest(c)] for c in db.constraints
        ]
        snapshot["~pcache"] = _pcache_signature(db)
        # Custom properties: rig-control sliders live here. json.dumps in
        # the path so names with dots/quotes survive the replica's parse
        # (db[json.loads(path[1:-1])]). Add/remove changes the path set →
        # structural escalation for free. Non-coercible values (nested
        # groups) are skipped — documented limitation.
        for prop_name, prop_value in db.items():
            if prop_name == identity.UUID_PROP or prop_name.startswith("_"):
                continue
            value = _coerce(prop_value)
            if value is not None:
                snapshot[f"[{json.dumps(prop_name)}]"] = value
        if db.pose is not None:
            # Full pose digest: any pose change (bone transforms, per-bone
            # constraints/bbone/idprops) escalates to a tier-2 resend of the
            # armature OBJECT — pose lives in the Object block, and armature
            # blobs are bones-only light. The sweep watches a LIGHT variant
            # (transforms+idprops): bone-slider idprop edits fire no
            # depsgraph event at all (probed 5.2).
            snapshot["~pose"] = _pose_digest(db.pose, full=True)
        try:
            # The eye toggle: per-view-layer state, not an RNA property —
            # rides as "@hide" and is applied via hide_set() on the replica.
            snapshot["@hide"] = db.hide_get()
        except RuntimeError:
            pass  # not in the active view layer
    if isinstance(db, bpy.types.Key):
        # Shape-key sliders: tier-1 deltas for instant feedback (the owning
        # mesh fires on the same edit and resends anyway — the blob just
        # trails these by its transfer time).
        for kb in db.key_blocks:
            base = f"key_blocks[{json.dumps(kb.name)}]"
            snapshot[f"{base}.value"] = kb.value
            snapshot[f"{base}.mute"] = kb.mute
    if isinstance(db, bpy.types.Scene):
        # Active camera is a pointer — rides as a name, applied by setter.
        snapshot["@scene_camera"] = db.camera.name if db.camera else ""
    if isinstance(db, bpy.types.Collection):
        # Membership is structural (link/unlink → tier-2 resend); the
        # view-layer pair (outliner checkbox + eye) applies by setter.
        snapshot["~members"] = sorted(
            [o.name for o in db.objects] + [c.name for c in db.children]
        )
        lc = _layer_collections().get(db.session_uid)
        if lc is not None:
            snapshot["@lc_exclude"] = lc.exclude
            snapshot["@lc_hide"] = lc.hide_viewport
    node_tree = getattr(db, "node_tree", None)
    if node_tree is not None:
        _walk_sockets(node_tree, snapshot)
    return snapshot


def _walk_sockets(node_tree, snapshot: dict) -> None:
    # node.path_from_id() is relative to the node TREE id; prefix so paths
    # resolve from the owning material/world/light datablock.
    for node in node_tree.nodes:
        base = f"node_tree.{node.path_from_id()}"
        for i, socket in enumerate(node.inputs):
            if socket.is_linked:
                link = socket.links[0]
                snapshot[f"~link.{base}.inputs[{i}]"] = (
                    f"{link.from_node.name}.{link.from_socket.identifier}"
                )
            if not hasattr(socket, "default_value"):
                continue
            value = _coerce(socket.default_value)
            if value is not None:
                snapshot[f"{base}.inputs[{i}].default_value"] = value


# ── module lifecycle ─────────────────────────────────────────────────────────

_sync: HostSync | None = None


@persistent
def _depsgraph_handler(scene, depsgraph):
    if _sync is not None:
        _sync.on_depsgraph(scene, depsgraph)


@persistent
def _load_post_handler(_filepath):
    if _sync is not None:
        if _DEBUG:
            print(f"qcb load_post → reset + bootstrap ({bpy.data.filepath})", flush=True)
        _sync.reset_for_new_file()


def _flush_timer():
    if _sync is None:
        return None
    return _sync.flush_tick()


def start(transport, paused_fn, mappings=()) -> HostSync:
    global _sync
    _sync = HostSync(transport, paused_fn, mappings)
    bpy.app.handlers.depsgraph_update_post.append(_depsgraph_handler)
    bpy.app.handlers.load_post.append(_load_post_handler)
    bpy.app.timers.register(_flush_timer, first_interval=_FLUSH_TICK, persistent=True)
    return _sync


def stop() -> None:
    global _sync
    _sync = None
    if _depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_handler)
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
