# Caches across the bridge — an audit

*2026-09-23. Blender 5.2.2 LTS, macOS. Every claim below marked **probed** was
measured with the scripts in `probes/caches/`; run them before trusting a
number on a different Blender.*

The question this answers: can the host→replica sync handle simulation
caches — legacy point caches, disk caches, geometry-nodes simulation zones and
bake nodes — better than "bake, then press Force Resync", for instance by
automatically toggling disk cache?

**Short answer: yes, but not with disk cache.** Toggling `use_disk_cache` is the
one thing that makes the replica *worse* — it is exactly the configuration that
freezes a sim on the replica today. The toggle that works is `use_external`
with a path on shared storage, set **before** the bake. Packed geometry-nodes
bakes already cross the wire and are used; nothing detects them. The rest of
this document is the evidence.

The addon's structure is described in `ARCHITECTURE.md` (on `main`; not yet
merged to the agent branch).

---

## 1. What the control system does with caches today

There is exactly one mechanism that moves cache data: the tier-3 bootstrap,
`wm.save_as_mainfile(copy=True, compress=True)` of the whole file
(`ring0/bootstrap.py:21-33`). Tier 1 carries property tuples, tier 2 carries a
`libraries.write` of one datablock, and the control lane carries hello/goodbye,
shot, zoom and heartbeats. None of them carries a cache frame, and the replica
never calls a bake, free, or rescan operator — `point_cache`, `is_baked`,
`ptcache` and `bake` do not occur in `replica_apply.py` at all.

### Detection

`_pcache_signature` (`ring0/host_handlers.py:650-673`) samples five fields —
tag, `is_baked`, `use_disk_cache`, `frame_start`, `frame_end` — for cloth,
soft body, particle systems and dynamic-paint canvas surfaces. It rides the
0.5 s sweep because a bake finishing on a job thread fires no dependable
depsgraph event. A change escalates the object to tier 2 via the `~pcache`
structural pseudo-path (`ring1/shadow.py:118-124`, `host_handlers.py:315-323`).
`bake_note` is set only when the *set of baked tags* changes
(`host_handlers.py:130-133`) and — since 2026-09-23 — cleared when the last
bootstrap chunk actually leaves, not when it is queued. `point_cache` is in `_DIGEST_SKIP` (`:464`), so the
modifier digest deliberately sees none of it — the signature is the only eye.

Not detected anywhere: `use_external` and `filepath` (omitted from the
signature), the rigid-body world cache (scene-level; zero references in the
repo), Mantaflow bake state, and **geometry-nodes bakes** — `NodesModifier.bakes`
is a COLLECTION and `_settings_digest` (`:490-530`) handles no collection
types, so baking a simulation zone changes nothing the host can see.

### Transport

A cache-bearing object escalates to tier 2, always; it never reaches tier 3 on
its own. The partial blend is the object and its dependencies. What that
carries is the subject of §2.

### Apply

`tier2_io.apply_blob` replaces the replica's object with the arrival and does
nothing cache-related afterwards. `bootstrap.apply_mainfile` writes the file to
`<tmp>/qcb-in-<tag>.blend`, opens it, and **deletes it** (`bootstrap.py:112-119`).
The tag changes on every bootstrap. Two consequences that matter below:

- `bpy.data.filepath` on the replica names a file that no longer exists, in
  the OS temp directory.
- Every `//`-relative path not in `_PATH_COLLECTIONS` (`bootstrap.py:44-47` —
  images, libraries, movieclips, sounds, fonts, volumes, `cache_files`)
  resolves against that vanished file. That includes Blender's implicit
  `//blendcache_<name>/`, `FluidDomainSettings.cache_directory`, and
  geometry-nodes `bake_directory`. None of `PointCache.filepath`,
  `cache_directory` or `bake_directory` is ever localized or path-mapped; they
  arrive in host form.

### Surface

The host panel says `⚠ sim bake changed (<obj>) — Force Resync ships it`. The
replica's overlay says nothing about caches. Force Resync is a full save, a
full transfer, and `open_mainfile` — manual by design (decision #8).

---

## 2. What actually crosses — probed

Method: a cloth grid baked on a *host* Blender process, then examined in a
*fresh* Blender process the way the replica would receive it: appended from a
`libraries.write` partial (tier 2), or opened from a full copy saved under a
different name and directory (tier 3, the bootstrap shape). Flags were
recorded, but the verdict in every row is **positional**: the cloth's lowest
vertex at frame 12 after a direct `frame_set(1)` → `frame_set(12)` jump, which
is how the replica seeks. Host baked value **−0.5346**. An unbaked cache on the
same jump gives **−0.6061** (one step from rest). No frames at all gives
**0.0**.

| shape | `is_baked` | `info` | z at 12 | reads the frames? |
|---|---|---|---|---|
| tier 2, memory cache | false | "12 frames in memory" | −0.6061 | **no** — re-simulated |
| tier 3 (renamed full copy), memory cache | true | "12 frames in memory" | −0.5346 | yes |
| tier 3 (renamed full copy), **disk** cache | true | "12 frames on disk" | **0.0** | **no — frozen at rest** |
| same basename, cache dir copied alongside | true | "12 frames on disk" | −0.5346 | yes |
| tier 2, **external** cache, absolute `filepath` | false | "No valid data to read!" | **−0.5346** | **yes** |
| the same, after a rescan | true | "6 points found!" | −0.5346 | yes |

Findings, each of which changes something:

1. **A memory cache that crosses tier 2 is inert.** The frames are in the
   blob (`info` says so) but arrive with `is_baked` false, and an unbaked
   cache re-simulates on a frame jump. Since the replica only ever jumps, a
   tier-2 memory cache buys nothing. This refines the 2026-09-18 note
   ("playback uses the frames"): sequentially, yes; the replica is never
   sequential.
2. **A disk cache is silently frozen on the replica after every Force
   Resync.** The renamed copy reports `is_baked: true` and "12 frames on
   disk", but the directory it reads from —
   `<tmp>/blendcache_qcb-in-boot-0-1/` — does not exist, and because the
   flag says baked, Blender does not simulate either. The result is the rest
   pose, with every indicator green. This is a **current bug** for any user
   with `use_disk_cache` on, and it gets worse, not better, with each resync.
3. **`is_baked` and `info` are serialized state, not a filesystem check.**
   Both lie after a rename. Nothing in the addon should trust them for
   liveness; the smoke suite's positional comparison is the right instinct.
4. **External caches simply work across tier 2.** The absolute `filepath`
   survives `libraries.write`, and Blender reads the frames from it on
   evaluation before any rescan; the rescan only restores the flag. This is
   the path-mappable form, and it is the mechanism everything below builds on.
5. **A safe rescan exists.** Reassigning `pc.filepath = pc.filepath` (or
   `frame_end`, or `name`) makes Blender rescan and set `is_baked` from the
   files. No `use_external` toggle is needed — which matters, because the
   2026-09-18 note warns that unchecking External can delete files. (The
   replica's toggle in the probe did not delete anything: 12 files before and
   after. The warning stands as a hazard to design around, not a
   disproven claim.)
6. **Converting an already-baked cache to external does not migrate its
   frames.** Both disk→external and memory→external left `is_baked` true,
   zero files at the new path, and z = 0.0 — a frozen sim. **The external
   path must be set before the bake**, or the conversion must trigger a
   re-bake and say so.
7. **An unbaked external cache on the replica writes into the shared
   directory and poisons the host's bake** (`probes/caches/shared_dir_*.py`,
   found by `run_smoke_cache`). Sequence: the replica receives the external
   path while the directory is still empty, evaluates a frame or two (it
   writes `_000001`, `_000002`), the host then bakes — and ends up with
   **2** files, not 24; the replica later reads its own two frames as
   "baked". With the replica keeping the cache in memory until frames exist
   on disk (`use_external = False` while the directory has no `.bphys`), the
   host bakes 24, the replica's jump simulates locally without writing, and
   after the settings resend it reads the host's value exactly (1.540838).
8. **On the replica, an appended external cache needs nothing but its path.**
   (`probes/caches/shared_dir_append.py`.) The arriving object reads every
   frame on every seek with `is_baked` false and the files untouched;
   assigning `filepath` (the same path, or a mapped alias) sets `is_baked`
   and is safe; toggling `use_external` off→on is safe. What **wipes the
   directory to 2 files** is re-setting `use_disk_cache`/`use_external` on a
   cache that is already external and already evaluated, then seeking. The
   replica therefore assigns the path, switches external on only when it had
   switched it off (finding 7), never touches the disk flag, and never
   re-seeks.

### Geometry nodes — probed

A cube with a simulation zone that lifts it 0.1 per frame; z at frame 10 after
a direct jump. Baked = **2.0**. Unbaked on the same jump = **1.2**.

| shape | z at 10 | uses the bake? |
|---|---|---|
| host, PACKED, baked | 2.0 | yes |
| tier 2 (partial write), PACKED | **2.0** | **yes** |
| tier 2, DISK with explicit absolute `bake_directory` | 1.2, `bake_directory` arrived **empty** | no — the directory does not survive `libraries.write` |
| tier 3 (renamed full copy), DISK, absolute directory | 2.0 | yes |
| tier 2, DISK, then `bake_directory` re-set on the replica | **2.0** | **yes — immediately, no rescan op** |

And on detection: the bake operator fires **one** `depsgraph_update_post`
naming every affected object with `is_updated_geometry=True` and no
transform; `simulation_nodes_cache_delete` the same. Scrubbing an *unbaked*
zone through twelve `frame_set`s fires **zero** updates. The `bakes` entries
are byte-identical before and after a bake — RNA cannot see it — so the
depsgraph update is the only signal, and it is a clean one.

Packed bakes are small: the partial blend grew by 1,174 bytes for ten frames
of an eight-vertex cube. DISK bakes land at
`<bake_directory>/<bake_id>/blobs/NNNNN_00000.blob` plus metadata — 20 files
for ten frames.

---

## 3. Per-system verdict

| system | today | what would make it work |
|---|---|---|
| Cloth / soft body / particles / dynamic paint, **memory** | detected; tier 2 carries inert frames; tier 3 works | external cache on shared storage, set before baking (§4) |
| the same, **disk** (`use_disk_cache`) | detected; **frozen on the replica after resync** — bug | same; and until then the panel should say the truth |
| the same, **external** (`filepath`) | *not* detected (fields omitted from the signature); path never mapped | add `use_external`/`filepath` to the signature; path-map `filepath`; rescan on the replica after apply |
| Rigid-body world cache | nothing anywhere; scene-level, so tier 2 cannot carry it (`t2_unsupported`) | out of reach of tier 2 by construction; external cache + path map would apply if it were detected; note as a gap |
| Fluid / Mantaflow | settings cross via the modifier digest, including a host-form `cache_directory`; no bake detection | path-map `cache_directory`; caches are always on disk, so this is the same shape as GN DISK. **Unprobed** — needs a fluid bake |
| GN simulation zone / bake node, **PACKED** | invisible to detection; **crosses tier 2 and is used** once sent | detect the bake from the depsgraph (§4) — the transport already works |
| GN, **DISK** | `bake_directory` in the digest but lost in the partial write; never path-mapped | carry `bake_directory` (and per-bake `directory` when `use_custom_path`) as a tier-1 path-mapped setter |
| Alembic / USD `CacheFile` | localized at bootstrap only; not on tier-2 arrivals; not in the uuid map; repointed without a reload | localize on tier 2 too; count as unmapped when unmapped |

Two adjacent findings that are not cache-specific but were found here:
`unmapped_paths` is counted on the replica and **never shown** — the overlay
and the pong status omit it, so the "unmapped paths surface, never silently"
promise (`bootstrap.py:65-66`) is not kept; and `bake_note` clears when the
bootstrap is *queued*, so the panel goes green while the file is still in
flight.

---

## 4. The strategy: externalize, path-map, rescan — opt in

The design that the evidence supports, in the order it should be built:

**A. Tell the truth first.** *(Done 2026-09-23: `replica_apply._count_frozen_caches` checks for `blendcache_<stem>/` beside the file; the count rides the overlay and the pong, and the host panel says "use an external cache path".)* On the replica, after any bootstrap or tier-2
arrival, check each point cache: `is_baked` with `use_disk_cache` and no
external path means "frozen — the frames are on the host's disk". Report it in
the replica status and the host panel instead of a green flag. This is the
cheapest change and it removes the silent failure.

**B. A shared cache root, set before baking.** *(Done 2026-09-23: `cache_root` preference; the host externalizes unbaked point caches under `<root>/<file>/<uuid>/<sim>` at session start and on the sweep, refuses on an unsaved file (Blender ignores disk cache there) and counts baked caches it left alone; `use_external`/`filepath` are in the signature; the replica keeps a pathless cache in memory until frames exist and assigns the mapped path when they do — findings 7 and 8. `smokes/run_smoke_cache.sh`: a host bake is a replica bake with no Force Resync, mean z at frame 20 identical, 5/5.)* A host-side preference —
"cache root on shared storage", one absolute directory on the mapped volume.
When enabled, the addon sets `use_disk_cache`, `use_external` and
`filepath = <root>/<project>/<object-uuid>/<cache-index>` on every point cache
it tracks, and adds `use_external`/`filepath` to `_pcache_signature`. The
replica path-maps `filepath` on arrival and reassigns it to trigger the
rescan. From then on a bake on the host is a bake on the replica after the
next 0.5 s sweep and a tier-2 hop that carries settings only — no Force
Resync, no frames on the wire, no host-tree writes beyond what Blender's own
disk cache would have done. **Opt-in**, because it changes where the user's
caches live, and **before the bake**, because conversion afterwards is
destructive (§2 finding 6): applying it to an already-baked cache must warn
"re-bake to populate".

This replaces the `use_disk_cache` idea with the thing it was reaching for.
`use_disk_cache` alone ties the files to the .blend's basename, which the
replica cannot share; `use_external` decouples them and makes the path a
property the addon already knows how to translate.

**C. Detect geometry-nodes bakes.** *(Done 2026-09-23: `_has_bake_nodes` + a geometry-only update on such an object escalates it to tier 2; the survey's `gn_sim_bake` row shows the replica using the packed bake at frame 10, 367 ms after the bake.)* For an object whose NODES modifier's tree
contains a simulation zone or bake node, treat a depsgraph update that is
`is_updated_geometry` without transform and produces an empty tier-1 diff as a
bake or delete, and escalate to tier 2. Playback contributes no updates, so it
cannot storm; a node edit already escalates through the socket snapshot. PACKED
bakes then cross and are used with no further work.

**D. Path-map the bake directory.** *(Done 2026-09-23: `bootstrap.localize_object_paths` maps `NodesModifier.bake_directory`, `bakes[i].directory` (custom paths), `FluidDomainSettings.cache_directory` and external `PointCache.filepath` at bootstrap and after every tier-2 arrival. Fluid remains unprobed.)* Carry `NodesModifier.bake_directory` (and
`bakes[i].directory` when `use_custom_path`) as tier-1 dynamic paths through
the mapping table, and apply them after tier-2 arrivals as well — setting the
directory on the replica was enough to make a DISK bake readable, with no
rescan. The same treatment fits `FluidDomainSettings.cache_directory` once a
fluid probe confirms it.

**E. Fix the adjacent honesty gaps** — surface `unmapped_paths`, localize
`CacheFile` on tier 2, clear `bake_note` on arrival rather than at queue.

What this does not do, and should not: transport frames over the wire. That
was ruled out for *missing media* on 2026-09-17 and the shared-storage premise
still holds; the strategy above needs only settings and paths to cross, which
is what the sync already carries well.

---

## 5. What is still unprobed

- Mantaflow: whether `cache_directory` survives a partial write, and whether a
  domain reads an absolute mapped directory without a rescan.
- The rigid-body world cache under an external path.
- Whether a `CacheFile` repointed on the replica needs an explicit reload
  (only `Image` gets one today).
- The "unchecking External deletes files" warning, against an external
  directory specifically — the probe never exercised that case, and the
  strategy avoids the toggle so it need not.
- Everything above on Windows, and on a real SMB share: the temp-then-rename
  and the rescan are the two things most likely to behave differently there.

## 6. Reproducing

```
S=<scratch>/pc P=<scratch> ; see probes/caches/README.md
```
Each script runs the "host" and "replica" halves in separate Blender
processes on purpose; a same-process test cannot show what crosses.
