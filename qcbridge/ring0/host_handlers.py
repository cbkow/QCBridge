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
    "curves", "images", "node_groups", "collections",
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
        tier-1 dirty on change; the normal flush diffs and ships them."""
        now = time.monotonic()
        for obj in bpy.data.objects:
            uuid = self.registry.uuid_for(obj.session_uid)
            if uuid is None:
                continue
            try:
                eye = obj.hide_get()
            except RuntimeError:
                eye = None  # not in the active view layer
            vector = (eye, obj.hide_viewport, obj.hide_render)
            if self._vis_state.get(uuid) != vector:
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
                        try:
                            eye = db.hide_get()
                        except RuntimeError:
                            eye = None
                        self._vis_state[uuid] = (
                            eye, db.hide_viewport, db.hide_render
                        )

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
            return  # first contact: peer has this state via tier-2/3
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
    # groups, images…): stamp + mark, no tier-1 table.
    return isinstance(db, (bpy.types.Mesh, bpy.types.Curve, bpy.types.NodeTree))


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


def build_snapshot(db: bpy.types.ID) -> dict:
    """Tracked values plus "~" pseudo-paths: structure signatures that are
    never sent as tier-1 writes — a change in one escalates to tier 2."""
    snapshot: dict[str, object] = {}
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
            [m.name, m.type, m.show_viewport] for m in db.modifiers
        ]
        try:
            # The eye toggle: per-view-layer state, not an RNA property —
            # rides as "@hide" and is applied via hide_set() on the replica.
            snapshot["@hide"] = db.hide_get()
        except RuntimeError:
            pass  # not in the active view layer
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
