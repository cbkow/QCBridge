# What reaches the replica — a coverage survey

*2026-09-23, branch `spike/quinn`, Blender 5.2.2 LTS, agent transport,
loopback.* Companion to `SYNC-AUDIT.md` (the mechanisms) and `CACHES.md`
(simulation caches). This one answers a single question per user action:
**does it reach the replica, and how fast?**

## Method

`smokes/coverage/run_coverage.sh` drives a live host↔replica pair through
`smokes/coverage/catalog.py`: 123 actions, each with a probe of the property
that matters, evaluated on both sides. The host records the probe right after
acting; the replica reports it four times a second; a match within the settle
window (2.5 s, i.e. the sweep plus the debounce plus a tier-2 flight) is
**crossed**. A match *after* the window is **piggybacked**: the edit itself was
not detected, a later action shipped the same datablock — or carried it as a
dependency (an empty's display type "arrived" 50 s late, inside the blob of a
hook modifier that pointed at it). Twelve piggybacked rows were re-run in
isolation, where nothing later could rescue them; all twelve stayed
undelivered, and those are the results shown. The full and isolated runs are
in `spikes/parity/results/2026-09-23-sync-coverage/`.

Actions mirror the UI: RNA writes fire the same update callbacks a slider
does; operators run under a window override. Where the API skips a tag the UI
would set, the action tags (noted). Undo is excluded — `ed.undo()` segfaults a
background Blender and rewinds the whole session in a GUI one.

## The inventory: what can cross at all

Every `bpy.data` collection against the addon's sets. *t1* = has tracked
property paths; *t2* = a whole-datablock resend exists; *swept* = deletions
are tombstoned; *paired* = a resend remaps over the replica's copy instead of
appending `.001`; *path* = filepaths are localized.

| collection | t1 | t2 | swept | paired | path |
|---|---|---|---|---|---|
| objects, lights, cameras, materials, worlds, collections | ✓ | ✓ | ✓ | ✓ | |
| scenes | ✓ | **✗** | ✓ | | |
| meshes, curves, node_groups, actions, lattices, armatures | | ✓ | ✓ | ✓ | |
| shape_keys | ✓ | (with owner) | ✓ | | |
| images | | **✗ (changes undetected)** | ✓ | ✓ | ✓ |
| cache_files, fonts, libraries, movieclips, sounds, volumes | | ✗ | ✗ | ✗ | ✓ |
| grease_pencils, hair_curves, lightprobes, metaballs, particles, pointclouds, textures, speakers, linestyles, masks | ✗ | ✗ | ✗ | ✗ | ✗ |

The last row is not as bad as it looks and not as good as the survey's
"other types" rows suggest: an object of any type crosses when it is *added*
(first contact ships the Object blob, data included), but a later edit of
that data has no detector.

## Results

Latency is host edit → visible on the replica, ms. Cause and fix are from the
code trace in `SYNC-AUDIT.md`; a cause in *italics* is inferred, not read.

### Objects and visibility

| action | result | ms | cause / fix |
|---|---|---|---|
| add (operator) | crossed | 402 | first contact → tier 2 |
| delete | crossed | 305 | 0.5 s sweep tombstone |
| rename | crossed | 448 | sweep + tier-1 `name` |
| duplicate | crossed | 325 | restamp + first contact |
| **swap the mesh under an object** | NOT crossed | | `data` untracked, no `~data` signature (A4) |
| **delta transforms** | NOT crossed | | `delta_*` not in `TRACKED["OBJECT"]` — add them |
| **display as wire** | NOT crossed | | `display_type` untracked |
| **show in front** | NOT crossed | | `show_in_front` untracked |
| colour | crossed | 324 | tier 1 |
| **empty display type/size** | NOT crossed | | untracked |
| **instance a collection** | NOT crossed | | `instance_type`/`instance_collection` untracked (A4) |
| vertex parenting | crossed | 380 | `~parent` |
| **light linking receiver** | NOT crossed | | `light_linking` untracked |
| nested custom prop | crossed | 473 | sweep idprops digest |
| eye toggle | crossed | 577 | sweep, `@hide` |
| hide_viewport | crossed | 737 | sweep |
| hide_render | crossed | 641 | sweep |
| **camera ray visibility** | NOT crossed | | `visible_*` untracked — a reviewer sees an object the render won't |
| **holdout / shadow catcher** | NOT crossed | | untracked |

### Mesh data

| action | result | ms | cause / fix |
|---|---|---|---|
| move a vertex | crossed | 380 | Mesh ID → tier 2 |
| vertex group + weights | crossed | 284 | tier 2 |
| add a UV map | crossed | 434 | tier 2 |
| add a colour attribute | crossed | 357 | tier 2 |
| shade smooth | crossed | 249 | tier 2 |
| face material index | crossed | 390 | tier 2 |
| **custom prop on the mesh datablock** | NOT crossed | | only Object idprops are swept |

### Modifiers

| action | result | ms | cause / fix |
|---|---|---|---|
| add subsurf | crossed | 292 | `~modifiers` |
| change a setting | crossed | 283 | settings digest |
| reorder | crossed | 428 | `~modifiers` |
| remove | crossed | 321 | `~modifiers` |
| viewport toggle | crossed | 229 | `~modifiers` |
| render toggle | crossed | 382 | digest |
| hook → empty | crossed | 277 | digest (pointer) |
| displace with a legacy texture | crossed | 404 | texture rides the blob |
| **change the legacy texture's setting** | NOT crossed | | `textures` outside every set; digest stores the pointer only |

### Materials, nodes, images

| action | result | ms | cause / fix |
|---|---|---|---|
| new material on a slot | crossed | 210 | Mesh ID |
| assign a different material to a slot | crossed | 230 | *Object data update* |
| socket default (Metallic) | crossed | 418 | tier-1 socket path — **and** a 64 KB Mesh blob rides along (below) |
| new link | crossed | 280 | `~link` structural |
| Math node operation, **node wired into the shader** | crossed | 391 | not via the material: the depsgraph flags the **Mesh** wearing it, and Mesh updates are unconditional tier 2 (`classifier.py:55`) — the material rides that blob |
| **the same node, dangling** | NOT crossed | | no shader change → no depsgraph propagation → nothing (node properties are not walked, A1) |
| mute a node (wired) | crossed | 386 | Mesh resend, as above |
| move a ColorRamp stop (wired) | crossed | 394 | Mesh resend |
| assign an image to an Image Texture (wired) | crossed | 432 | Mesh resend; dangling: piggybacked only |
| add a node | crossed | 376 | path-set change → `~` structural; `nodes.new()` alone skips the tag the node editor sets, so the action tags |
| render method / backface / displacement | crossed | 680 | tier 2 |
| viewport display colour | crossed | 308 | tier 2 |
| **custom prop on a material** | NOT crossed | | only Object idprops swept |
| **edit inside a node group** | NOT crossed | | the group had no users → the bootstrap save dropped it; with users it is a syncable NodeTree |
| **new generated image** | NOT crossed | | zero users → not saved; and Image changes are never detected (A3) |
| **paint a pixel** | NOT crossed | | A3 |
| image colourspace | crossed | 369 | not via the Image: the reload updates its users, and the **Mesh** wearing the material is resent whole (64 KB here), carrying the image |

### Lights and cameras

| action | result | ms | cause / fix |
|---|---|---|---|
| power, colour, radius | crossed | 200–390 | tier 1 |
| **point → area, shape, size** | NOT crossed | | `type` is tracked but the arrival keeps SQUARE/0.25: `shape`/`size` untracked |
| **cast shadow off** | NOT crossed | | `use_shadow` untracked |
| **area spread** | NOT crossed | | untracked |
| add a sun | crossed | 333 | first contact |
| focal length | crossed | 237 | tier 1 |
| **DoF focus object** | NOT crossed | | pointer; `dof.focus_object` untracked |
| **sensor fit / height** | NOT crossed | | untracked |
| **orthographic + scale** | NOT crossed | | `type`, `ortho_scale` untracked |
| **background image** | NOT crossed | | collection on Camera; no signature |
| scene camera switch | crossed | 269 | `@scene_camera` |
| **marker bound to a camera** | NOT crossed | | Scene data; no tier 2 for Scene |

### World and scene

| action | result | ms | cause / fix |
|---|---|---|---|
| background strength | crossed | 349 | socket path |
| **switch `scene.world`** | NOT crossed | | Scene pointer untracked (needs an `@scene_world` setter) |
| **world colour, nodes off** | NOT crossed | | `WORLD` tracks `name` only |
| render resolution | crossed | 371 | tier 1 |
| view transform + exposure | crossed | 281 | tier 1 |
| render engine | crossed | 429 | tier 1 |
| **frame range**, **fps** | NOT crossed | | untracked — the replica's playback range is wrong |
| **Cycles render samples** | NOT crossed | | only `preview_samples` tracked |
| **film transparent**, **motion blur**, **render region** | NOT crossed | | untracked |
| **units**, **gravity** | NOT crossed | | untracked |
| **custom prop on the scene** | NOT crossed | | not swept |
| **add a view layer**, **enable a pass** | NOT crossed | | Scene-embedded; no tier 2 for Scene |
| **compositor node** | NOT crossed | | `compositing_node_group` never walked |
| **tracked edit + Scene keyframe in one window** | NOT crossed | | **A2, now empirical**: `resolution_percentage = 33` plus a Scene keyframe → `escalate T2 Scene` → `UNSUPPORTED`; the 33 never arrived. The same edit alone, next: crossed in 317 ms |

### Collections

| action | result | ms | cause / fix |
|---|---|---|---|
| **new collection, object moved into it** | NOT crossed | | replica shows it in *both*: master-collection membership is Scene-embedded and `apply_blob` links parentless arrivals into `scene.collection` |
| exclude from view layer | crossed | 296 | `@lc_exclude` |
| collection eye | crossed | 703 | `@lc_hide` |
| hide_render | crossed | 356 | tier 1 |
| **instance offset** | NOT crossed | | untracked |
| **custom prop** | NOT crossed | | not swept |
| move between collections | crossed | 321 | `~members` |

### Animation and physics

| action | result | ms | cause / fix |
|---|---|---|---|
| keyframe insert, move, interpolation | crossed | 214–354 | Action ID → tier 2 |
| action swap | crossed | 403 | `~anim` |
| push to NLA strip | crossed | 292 | `~anim` track count |
| **mute an NLA track** | NOT crossed | | `~anim` hashes the track *count* only |
| driver add, edit | crossed | 233–333 | `~anim` |
| rigid body on an object | crossed | 372 | Object blob |
| **rigid body world settings** | NOT crossed | | `scene.rigidbody_world` never referenced — and it is `None` on the replica, so the rigid body above cannot simulate there |
| **force field** | NOT crossed | | `obj.field` in no signature |
| particle system add | crossed | 353 | `~modifiers` |
| **particle settings change (count)** | NOT crossed | | `ParticleSettings` outside every set; digest stores the pointer |
| cloth mass | crossed | 396 | digest |
| fluid domain | crossed | 268 | digest |

### Other object types and files

| action | result | ms | cause / fix |
|---|---|---|---|
| curve bevel, point move; text body | crossed | 311–470 | Curve ID → tier 2 |
| metaball, grease pencil (+layer), volume, hair curves, point cloud, light probe, image empty — **added** | crossed | 272–467 | first contact ships the Object with its data |
| the same — **edited later** (metaball radius, GP layer, probe distance) | NOT crossed | | no detector for these data types (inventory) — add, yes; edit, no |
| **link an object from another .blend** | NOT crossed | | linked IDs are never stamped (`identity.is_stampable`) |

## Totals

Of 122 surveyed actions: **72 crossed on their own, 50 did not** (undo
excluded). Median latency of the ones that crossed: ~350 ms; the sweep-path
ones 577–737 ms.

## The cost hiding in the "crossed" column

Every material edit that changes the evaluated shader — a slider, a mute, a
ColorRamp stop, an image — resends **every Mesh that wears the material,
whole**. 64 KB here for a four-vertex plane; a production mesh is the full
mesh, serialized on the host main thread and applied indivisibly on the
replica, per slider tick. It is also *why* node property edits appear to
cross: the material is a dependency of that blob. The fix is the same as
SYNC-AUDIT D7: honour `is_updated_geometry` on Mesh updates (a shading-only
update should mark the material dirty, not the mesh), and walk node
properties so the material can go tier 1 or tier 2 on its own.

## What the holes have in common

Almost every miss is one of six causes, which is good news for the fix list:

1. **A short tracked-path table.** Object, Light, Camera, World and Scene each
   track a handful of paths; everything else on those datablocks is invisible
   unless it happens to sit inside a `~` signature. The fix is a longer table,
   and the survey is the acceptance test for it.
2. **Only Object custom props are swept.** Mesh, Material, Collection and
   Scene idprops need the same digest.
3. **Scene has no tier 2.** Frame range, fps, markers, view layers, passes,
   compositor, world pointer, gravity, units — all Scene-embedded, all lost.
   Either a Scene tier-2 path (its own `libraries.write` with the replica's
   scene remapped over it) or a much wider Scene tier-1 table plus `@` setters
   for the pointers.
4. **Node trees are walked for sockets and links only.** Node properties,
   mute, ColorRamp stops and image pointers cross today only as a side effect
   of the Mesh resend above — and not at all on nodes that don't reach the
   output, which is exactly the state a node is in while being set up.
5. **Pointer-only digests.** `texture`, `particle settings`, `focus_object`
   are recorded as pointers; a change *inside* the pointee is invisible.
6. **Datablock types outside every set** (inventory last row), plus Images.

The remaining singletons: linked libraries (never stamped), orphan datablocks
(not the sync's fault, but the panel should say "unused datablocks won't
cross"), the master-collection membership duplicate, and the rigid-body world.

## Reproducing

```
QCB_TRANSPORT=agent QCB_AGENT=spawn smokes/coverage/run_coverage.sh
QCB_COV_ONLY=key1,key2 QCB_COV_SETTLE=4 ...   # isolate rows
QCB_COV_UNDO=1 QCB_COV_ONLY=undo_after_move ...  # GUI only
```
Prints the table, writes `coverage.json`. Adding an action is one function
with a `@probe`; the survey is meant to grow with every fix.
