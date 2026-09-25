# Design notes

The settled findings the code cites. Each label below is referenced from a
comment somewhere in `qcbridge/`, `agent/` or `smokes/` as `DESIGN-NOTES
<section> <label>`; the measurements and the reasoning behind them were
working documents and are not part of the repository.

## Sync

How an edit travels: tier 1 is a small delta for a tracked property
(location, a shader socket, a view-layer flag), tier 2 is the whole datablock
written with `bpy.data.libraries.write`, and the hot lane carries the frame
number and the camera. A 0.25 s sweep catches what the depsgraph does not
report.

- **§1 latency.** On loopback, one machine, agent transport: a tier-1 delta
  reaches the replica in ~180 ms p50, a tier-2 blob in ~220 ms, a frame
  change in ~35 ms, a sweep-only edit in ~180 ms since C2 (was ~500 ms).
  `smokes/bench_latency.sh` reproduces the numbers.
- **§3.** Findings are ranked by what a reviewer would suffer: A = silently
  wrong, B = stalls and drops, C = slow, D = wasteful.
- **§4.2 honesty.** The panel and the burn-in say who we are, what we want
  and what went wrong; an unmapped path or a dropped frame is counted and
  shown, never swallowed.
- **A1.** Shader-node property edits are invisible to the material's own
  diff (only socket values and links are walked); wired nodes still reach
  the replica because the mesh wearing the material is resent (D7).
- **A2.** A tier-1 edit that shares a diff with a Scene structural change
  must still go out: the Scene has no tier 2, so its tier-1 half is sent
  even when the structural blob is refused.
- **A4.** `object.data` re-links, material slots and `instance_collection`
  are tracked (`~data`, `~materials`, `@instance_collection`); swapping the
  mesh under an object is not an empty diff.
- **A5.** View-layer state (`@hide`, `@lc_exclude`, `@lc_hide`) is not in a
  tier-2 blob; the replica keeps the last `@` values per uuid and re-applies
  them after `apply_blob`, so a hidden object stays hidden after a resend.
- **A6.** A cold frame the agent cannot deliver is dropped and credited; the
  agent reports `cold_dropped`, the panel counts it, and every reconnect
  re-bootstraps, because the replica may be a new process.
- **A9.** The replica is not read-only by construction: a replica-side
  depsgraph handler counts updates on stamped datablocks it did not write,
  and the count rides the pong so the host can say so.
- **B3.** Tier-2 transfer resumes from the refused chunk; credits are bytes
  (a 32 MiB window), so chunk count does not matter.
- **B4.** Control, acks and events have their own queue in the agent's link,
  drained ahead of cold frames, so a big transfer cannot trip the 3 s
  liveness window.
- **C2.** The sweep runs every 250 ms and sweep-marked datablocks skip the
  debounce.
- **D1.** The pose digest ignores `matrix_basis` on bones the action or NLA
  animates; scrubbing a rig sends no rig blobs.
- **D2.** `serialize` flushes the edit-mesh first, and a blob byte-identical
  to the last one sent for that datablock (plus its point-cache signature)
  is skipped.
- **D4.** Big blobs are not concatenated on the main thread: the link writes
  buffers in turn and the reassembler returns a view of its buffer; chunks
  are 4 MiB, lanes accept 256 MiB.
- **D6.** `build_snapshot`'s layer-collection walk is memoised for 50 ms.
- **D7.** A shading-only update on a geometry type does not resend the
  mesh; `is_updated_geometry` is honoured.

## Caches

- **§2 finding 5.** Reassigning `pc.filepath = pc.filepath` (or `frame_end`,
  or `name`) makes Blender rescan a point cache and set `is_baked` from the
  files. No `use_external` toggle is needed; toggling it can delete files.
- **§2 finding 6.** Converting an already-baked cache to external does not
  migrate its frames: the cache reads as baked with zero files at the new
  path, a frozen sim. The external path must be set before the bake.
- **§2.** A memory cache that crosses tier 2 is inert; a disk cache is frozen
  on the replica after a resend; a geometry-nodes bake is invisible to RNA
  and crosses only as the packed bake on the object.
- **§4 B.** A shared cache root, set before baking: the host externalises
  unbaked point caches under `<root>/<file>/<uuid>/<sim>` at session start
  and on the sweep, refuses on an unsaved file, and counts baked caches it
  leaves alone. The replica maps the path and reassigns it (finding 5).
- **§4 D.** `NodesModifier.bake_directory`, custom bake directories,
  `FluidDomainSettings.cache_directory` and external `PointCache.filepath`
  are mapped through the mapping table at bootstrap and after every tier-2
  arrival.

## Coverage

A survey of 122 user actions on loopback: 120 reach the replica. The
catalog is `smokes/coverage/catalog.py`; adding an action is one function
with a `@probe`. The misses shared six causes: a short tracked-path table,
custom props swept on Objects only, no Scene tier 2, node trees walked for
sockets and links only, pointer-only digests, and datablock types outside
every set.

- **Cause 5, pointer-only digests.** `texture`, `particle settings`,
  `focus_object` were recorded as pointers, so a change inside the pointee
  was invisible; the owner is re-shipped when the pointee changes.
- **The inventory.** Types that cross only as a side effect of their owner
  now have their own tier-2 path.

## Agent

Decisions of 2026-09-24, with the owner:

- **§8.** One agent per machine with a Send scene / Receive scene switch,
  live without a restart. The settings window is the tray's UI: this
  machine, pairing, storage, stream, diagnostics. Blender's preferences are
  a read-out of the agent. Every path field has a folder picker that
  proposes the other platform's form from the mount table. The token lives
  in the OS keychain and only its fingerprint is shown.
